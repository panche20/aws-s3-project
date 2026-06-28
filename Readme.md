<img width="1106" height="481" alt="image" src="https://github.com/user-attachments/assets/bb9f721f-6670-499f-9509-a1750c8d74db" /># Event-Driven Serverless Image Processing Pipeline

**Stack**: S3 (raw) → S3 Event Notification → Lambda (Pillow) → S3 (processed)

**Region used below**: us-east-1 (swap for your preferred region — bucket names must be globally unique, so replace <suffix> with something like your account ID or initials everywhere)

## PART 1 — The Engineering Deep Dive

**The 'Why' (The Problem)**

Before event-driven architectures existed, image processing was handled one of two bad ways:

- **Synchronous processing on the request path** — a user uploads a photo to your web app, and your API server itself calls a resize library before returning a response. This ties up a web server thread/process for CPU-heavy work, kills your request latency (P99 spikes), and doesn't scale — if 10,000 users upload at once, your API tier falls over.
- **A polling worker/cron job** — a script runs every N minutes, scans the bucket/folder for new files, and processes them. This wastes compute when there's nothing to do, introduces processing lag (up to N minutes), and the polling script itself becomes a single point of failure you have to keep alive and patch.

The pain point: **compute and storage events are decoupled from a workflow trigger**. You need infrastructure that says "the instant a new object lands here, react to it" — without you running a server 24/7 to watch for it.

This is a **must-have pattern** because almost every modern SaaS product (Slack avatars, Instagram posts, e-commerce product photos, document thumbnails in Drive/Dropbox-like apps) needs exactly this: ingest → transform → serve, fully decoupled, scaling to zero when idle and to thousands of concurrent ops during a traffic spike.

### Deep-Dive Mechanics
1. **S3 Event Notifications — how the trigger actually works**

S3 isn't "watched" by Lambda. S3 itself emits an event internally when an object is created (PUT, POST, COPY, multipart complete). You configure a notification configuration on the bucket (an XML/JSON config stored as bucket metadata) that says "for events matching s3:ObjectCreated:* and prefix uploads/, invoke this Lambda ARN." S3 pushes the event — Lambda doesn't pull it.
The event payload Lambda receives is **small and metadata-only**:

```
{
  "Records": [{
    "s3": {
      "bucket": {"name": "..."},
      "object": {"key": "uploads/photo.jpg", "size": 204800}
    }
  }]
}
```

Critically: **the actual image bytes are NOT in the event**. Your Lambda code has to call s3.get_object() itself to fetch the bytes. This matters because Lambda's synchronous invoke payload limit is 6MB — but since the event is just JSON metadata, that limit is irrelevant here even for huge images. This is a classic interview trap (see below).

2. **Lambda's async invocation model for S3 triggers**

S3 invokes Lambda **asynchronously**. Lambda places the event on an internal queue, then a Lambda worker picks it up. This gives you:

- **At-least-once delivery** — S3 guarantees the event fires, but in rare failure scenarios (throttling, internal retries) the same event can be delivered twice. Your function must be idempotent — re-processing the same image and overwriting the same output key should be safe, which our design below ensures naturally.
- **Built-in retry** — if your function errors or times out, Lambda retries the async invocation (up to 2 more times by default) before optionally routing to a Dead Letter Queue (DLQ) or on-failure destination.

3. **Lambda execution environment internals**

Each invocation runs inside a microVM (Firecracker). On a cold start, AWS has to: provision the microVM, download your code package + layers, initialize the language runtime, run your code outside the handler (imports, global scope) — then run the handler. Subsequent invocations on a warm environment skip all of that and jump straight to the handler, reusing the same /tmp disk and global variables. This is why heavy imports (like from PIL import Image) should sit at module/global scope — they only pay the cost once per container lifecycle, not per invocation.

4. **The actual image transform (Pillow)**

Pillow decodes the image into an in-memory bitmap (raw pixel array), applies the resize algorithm (we use LANCZOS resampling — a high-quality interpolation kernel that filters out aliasing when downscaling), then re-encodes to JPEG with a quality parameter that controls the DCT/quantization compression Lambda — this is where your actual file-size reduction comes from, not just the pixel resize.

5. **IAM — the permission chain**

Two separate trust relationships have to exist:

- **The Lambda execution role** must have s3:GetObject on the source bucket and s3:PutObject on the destination bucket (this controls what Lambda's code can do).
- The S3 bucket must have **resource-based permission** granting lambda:InvokeFunction to the s3.amazonaws.com service principal, scoped to that specific bucket ARN (this controls who can invoke the function). Forgetting this second one is the #1 reason people's S3→Lambda triggers silently don't fire.

**- The Alternative Landscape**

<img width="1387" height="332" alt="image" src="https://github.com/user-attachments/assets/a6fea6a0-6454-4b7b-aa84-174e93ed862d" />

**Why Lambda direct wins here**: image resize is CPU-bound but fast (sub-second to low-single-digit seconds per image), bursty (uploads aren't constant), and stateless. You get true scale-to-zero billing and zero servers to patch. You'd graduate to SQS-buffered Lambda or Fargate/Batch only when you need backpressure control, video-length processing, or guaranteed ordering.

**Interview POV & Edge Cases**

**How it's asked**: "Design a system where users upload profile pictures that get resized into 3 thumbnail sizes." or "Walk me through what happens, end-to-end, when a file lands in S3 and triggers Lambda." Interviewers are listening for the event flow, the IAM split (execution role vs resource policy), and whether you mention idempotency/retries unprompted.

### Gotchas senior engineers are expected to know:

- **Infinite recursion loop** — if your Lambda writes its output back to the same bucket it's triggered on (even with a different prefix), and your trigger isn't prefix-filtered correctly, you create a self-triggering loop that runs until you hit account limits or rack up a huge bill. Always use a separate destination bucket (as we do) or a tightly scoped prefix filter, and ideally both.
- **6MB payload limit confusion** — candidates often think large images will break the trigger. They won't — the event is tiny; only synchronous Lambda request/response payloads are capped at 6MB. S3 GetObject/PutObject inside your code has no such limit (well into GBs, using multipart for very large files).
- **At-least-once delivery → duplicate invocations** — your function must tolerate processing the same key twice without corrupting state. Overwriting the same output key (as our design does) is naturally idempotent.
- **Cold starts under burst load** — if 5,000 images upload simultaneously, Lambda will scale out aggressively (subject to your account's concurrency limit, default 1,000 concurrent executions per region), but each new concurrent execution beyond the warm pool pays a cold start. For latency-sensitive paths, provisioned concurrency mitigates this (at a cost).
- **Memory ↔ CPU ↔ cost tradeoff** — Lambda allocates CPU proportionally to configured memory. Counter-intuitively, bumping memory from 256MB to 1024MB can make a CPU-bound image resize run 3–4x faster, finishing in less billed-duration — sometimes making the higher memory setting cheaper overall. Senior engineers are expected to load-test and tune this, not just pick the default.
- **/tmp ephemeral storage limit** — default 512MB, configurable up to 10GB. If you're processing very large images or batches in one invocation, you can hit OSError: No space left on device if you don't account for this.
- **Poison pill files** — a corrupted/non-image file uploaded by mistake will throw on Image.open() and retry 2 more times by default, then optionally land in a DLQ. Without a DLQ configured, it just vanishes — a silent failure. Production systems always wire up an on-failure destination (SQS or SNS).
- **Missing resource policy** — the single most common "my trigger doesn't fire" bug in real interviews and real production: the execution role has S3 access, but nobody granted s3.amazonaws.com permission to invoke the function.

### The 'Better Way' (Evolution)

- **S3 Object Lambda flips the model**: instead of precomputing every size on upload (wasting storage/compute on variants nobody ever requests), you intercept the GetObject call and transform on-the-fly, cached by CloudFront. Better for unpredictable, long-tail size requirements.
- **CloudFront + Lambda@Edge / CloudFront Functions** for resizing based on URL query params (?w=300) at the edge, closest to the user — this is effectively how Cloudinary/Imgix work under the hood.
- **AWS's official "Serverless Image Handler" solution** (CloudFormation-deployable) bundles CloudFront + Lambda + Sharp (Node, faster than Pillow for large batches) and is the production-grade reference architecture AWS itself recommends.
- **Lambda container images instead of zip+layer**, when you outgrow the 250MB unzipped layer limit (e.g., bundling libvips or ffmpeg alongside Pillow).
- **EventBridge instead of direct S3** notification when you need to fan the same upload event out to multiple independent consumers (e.g., resize AND run content moderation AND log to an analytics pipeline) without each one needing its own S3 notification config — S3 can only have one notification destination per event type per bucket without EventBridge in the loop.

- ## PART 2 — Architecture

```
[User/Client]
     │  PUT image
     ▼
[S3 Bucket: chetan-image-raw-<suffix>]   (prefix: uploads/)
     │  s3:ObjectCreated:* event
     ▼
[Lambda: image-resize-processor]  (Pillow layer attached)
     │  GetObject (raw)  →  resize + compress  →  PutObject (processed)
     ▼
[S3 Bucket: chetan-image-processed-<suffix>]
     ├── thumbnail/  (150x150)
     └── medium/     (800x800)
```

## PART 3 — Build It (GUI + CLI)
**Prerequisites**

- AWS CLI v2 configured on your EC2 Ubuntu instance (aws configure or an instance role)
- Docker installed on the EC2 instance (for building the Pillow layer against the correct Lambda runtime binaries)
- Your AWS Account ID: run aws sts get-caller-identity --query Account --output text

### Step 1 — Create the two S3 buckets
**GUI:**

S3 Console → Create bucket
Name: chetan-image-raw-<suffix>, Region: US East (N. Virginia), leave Block Public Access ON, Create
Repeat for chetan-image-processed-<suffix>

**CLI:**

```
aws s3api create-bucket --bucket chetan-image-raw-<suffix> --region us-east-1
aws s3api create-bucket --bucket chetan-image-processed-<suffix> --region us-east-1
```

### Step 2 — Write the Lambda function code
On your EC2 instance:

```
mkdir -p ~/image-processor && cd ~/image-processor
```

*lambda_function.py:*

```
import boto3
import os
import io
import urllib.parse
from PIL import Image

s3 = boto3.client('s3')

DEST_BUCKET = os.environ['DEST_BUCKET']
SIZES = {
    'thumbnail': (150, 150),
    'medium': (800, 800)
}
QUALITY = 80

def lambda_handler(event, context):
    for record in event['Records']:
        src_bucket = record['s3']['bucket']['name']
        src_key = urllib.parse.unquote_plus(record['s3']['object']['key'])

        # Defense-in-depth: never reprocess our own output even if buckets were misconfigured
        if src_key.startswith(('thumbnail/', 'medium/')):
            continue

        try:
            obj = s3.get_object(Bucket=src_bucket, Key=src_key)
            image_bytes = obj['Body'].read()

            with Image.open(io.BytesIO(image_bytes)) as img:
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')

                file_root = os.path.splitext(os.path.basename(src_key))[0]

                for size_name, dimensions in SIZES.items():
                    resized = img.copy()
                    resized.thumbnail(dimensions, Image.LANCZOS)

                    buffer = io.BytesIO()
                    resized.save(buffer, format='JPEG', quality=QUALITY, optimize=True)
                    buffer.seek(0)

                    dest_key = f"{size_name}/{file_root}.jpg"
                    s3.put_object(
                        Bucket=DEST_BUCKET,
                        Key=dest_key,
                        Body=buffer,
                        ContentType='image/jpeg'
                    )
                    print(f"Wrote {dest_key} ({resized.size}) to {DEST_BUCKET}")

        except Exception as e:
            print(f"ERROR processing s3://{src_bucket}/{src_key}: {e}")
            raise

    return {'statusCode': 200, 'body': 'OK'}
```
