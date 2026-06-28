# Event-Driven Serverless Image Processing Pipeline

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

### Step 3 — Package Pillow as a Lambda Layer

Pillow has compiled C extensions, so it must be built for Amazon Linux (Lambda's runtime), not your local OS — building inside the official AWS SAM build image guarantees a compatible binary.
**CLI (no GUI equivalent for this step — layers are CLI/build-tool territory):**

```
cd ~/image-processor
mkdir -p lambda-layer/python

docker run --rm -v "$PWD/lambda-layer":/var/task \
  public.ecr.aws/sam/build-python3.12 \
  pip install pillow -t /var/task/python

cd lambda-layer
zip -r ../pillow-layer.zip python
cd ..

aws lambda publish-layer-version \
  --layer-name pillow-layer \
  --zip-file fileb://pillow-layer.zip \
  --compatible-runtimes python3.12 \
  --region us-east-1
```

Note the *LayerVersionArn* in the output — you'll need it in Step 5.
Shortcut: the Klayers project publishes public Pillow layer ARNs per region/runtime if you want to skip the Docker build.

### Step 4 — Deep Dive: Create the IAM Execution Role

Before touching the console or CLI, you need to understand what you're actually building and why — because IAM is the #1 source of Lambda debugging pain.

**The Concept: Two Separate IAM Objects**
A Lambda execution role is composed of two distinct policy documents that solve two completely different questions:

```
┌─────────────────────────────────────────────────────────────────┐
│                     IAM Execution Role                          │
│                                                                 │
│  ┌─────────────────────────┐  ┌──────────────────────────────┐ │
│  │     TRUST POLICY        │  │     PERMISSIONS POLICY       │ │
│  │  (Role Trust Relation)  │  │   (Identity-based Policy)    │ │
│  │                         │  │                              │ │
│  │  "WHO can ASSUME        │  │  "WHAT AWS actions can       │ │
│  │   this role?"           │  │   this role PERFORM?"        │ │
│  │                         │  │                              │ │
│  │  Answer: Lambda         │  │  Answer: GetObject on        │ │
│  │  service principal      │  │  raw bucket, PutObject on    │ │
│  │  (lambda.amazonaws.com) │  │  processed bucket, write     │ │
│  │                         │  │  CloudWatch Logs             │ │
│  └─────────────────────────┘  └──────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

**Why does this two-part design exist?**

AWS IAM implements the principle of least privilege through separation of concerns. The trust policy says "Lambda is allowed to wear this costume (the role)." The permissions policy says "and while wearing this costume, here's what you're allowed to touch." Without BOTH, the system is broken — Lambda either can't assume the role, or assumes it but gets AccessDenied on every API call.

**Understanding the Policies Line-by-Line**
Trust Policy

```
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

<img width="882" height="437" alt="image" src="https://github.com/user-attachments/assets/e564ce69-173a-4d63-8255-51bbd36ebae2" />

**Why lambda.amazonaws.com and not your account/user?** Lambda runs your code on AWS-managed infrastructure — not on any specific EC2 instance or IAM user that you own. By trusting the service principal, you're saying "any Lambda invocation that is explicitly configured with this role is allowed to use it." The binding from "this specific function" to "this specific role" happens when you create/configure the function, and that binding itself requires your IAM user to have iam:PassRole permission — that's another layer of the chain of trust.

**Permissions Policy**

```
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadRawBucket",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::chetan-image-raw-<suffix>/*"
    },
    {
      "Sid": "WriteProcessedBucket",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::chetan-image-processed-<suffix>/*"
    },
    {
      "Sid": "WriteLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    }
  ]
}
```
Each statement explained:
**Statement 1 — ReadRawBucket:**

- s3:GetObject only — not s3:ListBucket, not s3:DeleteObject. Lambda code only needs to read the uploaded file. Giving it s3:* would violate least privilege — if your code ever had a bug or was compromised, it     couldn't accidentally delete your raw uploads or modify other objects.
- Resource ends with /* — S3 ARNs for object-level actions need the /* wildcard because arn:aws:s3:::bucket-name refers to the bucket resource, while arn:aws:s3:::bucket-name/* refers to the objects inside it.     This is a very common IAM/S3 gotcha. GetObject is an object-level action — it will always be denied if you forget the /*.

**Statement 2 — WriteProcessedBucket:**

s3:PutObject only on the destination bucket. Note we do NOT give PutObject on the source bucket — this is a defense-in-depth measure against the infinite loop bug described in the interview gotchas section.

**Statement 3 — WriteLogs:**

- Without these three actions, Lambda cannot write to CloudWatch Logs at all — every print() in your code and every Lambda platform message silently disappears. CreateLogGroup and CreateLogStream are needed on     the first invocation to create /aws/lambda/image-resize-processor. PutLogEvents is needed on every invocation to write log entries.
- We use "Resource": "arn:aws:logs:*:*:*" here (wildcard account/region) for simplicity. In production you'd scope it to arn:aws:logs:us-east-1:<account_id>:log-group:/aws/lambda/image-resize-processor:*.
- The AWS managed policy AWSLambdaBasicExecutionRole is exactly these three log actions — many tutorials attach that managed policy instead of writing them inline. Both work identically. Inline is more explicit    and educational.

**Method A — GUI (Console)**
**A1. Navigate to IAM**
Services menu (top-left) → search IAM → open IAM Console.
In the left sidebar: Roles → Create role (top-right blue button).

**A2. Select Trusted Entity**
You'll see four tiles:

```
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│   AWS service   │  │ AWS account     │  │   Web identity  │  │    SAML 2.0     │
│   ← PICK THIS  │  │                 │  │                 │  │    federation   │
└─────────────────┘  └─────────────────┘  └─────────────────┘  └─────────────────┘
```

Select **AWS service**.
Under "Use case", in the **Service or use case** dropdown, scroll to and select **Lambda**.
This auto-generates the trust policy with lambda.amazonaws.com as the principal — exactly the JSON we saw above. You can click "View policy document" to verify it matches.
Click **Next**.

**A3. Attach Permissions Policies (Skip for now)**
The console shows you a list of AWS managed policies to attach. Do not attach anything here. We're going to write a precise inline policy instead of attaching a broad managed policy like AmazonS3FullAccess.
Click **Next** without selecting any policies.

**A4. Name and Create the Role**

- Role name: image-processor-role
- Description: Execution role for image-resize-processor Lambda — S3 read/write and CloudWatch Logs
- Tags (optional but good habit): Project = image-pipeline, ManagedBy = manual

Review the trust policy shown at the bottom — it should show lambda.amazonaws.com as the trusted entity.
Click **Create role**.

**A5. Add the Inline Permissions Policy**
The role now exists but has zero permissions. Find it:
IAM → Roles → search image-processor-role → click it.
On the role detail page:

- **Permissions tab → Add permissions dropdown → Create inline policy**

You'll see a visual editor with dropdowns. Switch to JSON mode immediately — click the JSON tab at the top of the editor.
Paste the full permissions policy JSON:

```
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadRawBucket",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::chetan-image-raw-<suffix>/*"
    },
    {
      "Sid": "WriteProcessedBucket",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::chetan-image-processed-<suffix>/*"
    },
    {
      "Sid": "WriteLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    }
  ]
}
```
Click **Next** → Policy name: **image-processor-permissions** → Create policy

**A6. Verify in Console**
Back on the role detail page you should see:

```
Permissions policies (1)
┌──────────────────────────────┬─────────────┬─────────────────┐
│ Policy name                  │ Type        │ Attached via    │
├──────────────────────────────┼─────────────┼─────────────────┤
│ image-processor-permissions  │ Inline      │ Direct          │
└──────────────────────────────┴─────────────┴─────────────────┘

Trust relationships:
lambda.amazonaws.com  → sts:AssumeRole
```

Copy the ARN from the top of the page — you'll need it in Step 5:

```
arn:aws:iam::<account_id>:role/image-processor-role
```

**Method B — CLI (Full walkthrough)
B1. Create the files**
On your EC2 instance:

```
cd ~/image-processor
```

Create the trust policy file:

```
cat > trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF
```

Create the permissions policy file — replace <suffix> with your actual suffix before running:

```
SUFFIX="your-suffix-here"   # e.g., SUFFIX="chetan123"

cat > permissions-policy.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadRawBucket",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::chetan-image-raw-${SUFFIX}/*"
    },
    {
      "Sid": "WriteProcessedBucket",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::chetan-image-processed-${SUFFIX}/*"
    },
    {
      "Sid": "WriteLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    }
  ]
}
EOF
```

Verify both files look correct before proceeding:

```
cat trust-policy.json
cat permissions-policy.json
```

**B2. Create the role (attaches the trust policy)**

```
aws iam create-role \
  --role-name image-processor-role \
  --assume-role-policy-document file://trust-policy.json \
  --description "Execution role for image-resize-processor Lambda" \
  --tags Key=Project,Value=image-pipeline Key=ManagedBy,Value=cli \
  --region us-east-1
```

Expected output (abbreviated):

```
{
    "Role": {
        "Path": "/",
        "RoleName": "image-processor-role",
        "RoleId": "AROAXXXXXXXXXXXXXXXXX",
        "Arn": "arn:aws:iam::<account_id>:role/image-processor-role",
        "CreateDate": "2026-06-25T...",
        "AssumeRolePolicyDocument": {
            "Version": "2012-10-17",
            "Statement": [...]
        }
    }
}
```
Save the ARN — you'll use it in Step 5:

```
ROLE_ARN=$(aws iam get-role \
  --role-name image-processor-role \
  --query 'Role.Arn' \
  --output text)
echo $ROLE_ARN
```

**B3. Attach the permissions as an inline policy**

```
aws iam put-role-policy \
  --role-name image-processor-role \
  --policy-name image-processor-permissions \
  --policy-document file://permissions-policy.json
```

This command produces no output on success (exit code 0 = success). Verify:

```
echo $?   # should print 0
```

**B4. Verify everything is correct**
Check trust policy:

```
aws iam get-role \
  --role-name image-processor-role \
  --query 'Role.AssumeRolePolicyDocument' \
  --output json
```

Expected:

```
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "Service": "lambda.amazonaws.com"
            },
            "Action": "sts:AssumeRole"
        }
    ]
}
```

Check the inline permissions policy:

```
aws iam get-role-policy \
  --role-name image-processor-role \
  --policy-name image-processor-permissions \
  --query 'PolicyDocument' \
  --output json
```

Expected: the three-statement JSON you created above, with your actual bucket names interpolated in.
List all policies attached to the role (confirms nothing extra was accidentally added):

```
aws iam list-role-policies \
  --role-name image-processor-role
```

```
{
    "PolicyNames": [
        "image-processor-permissions"
    ]
}
```

**Common Mistakes at This Step
Mistake 1: Using AmazonS3FullAccess managed policy**

Convenient but grants s3:* on * — your Lambda role can now read, write, and delete every bucket in your account. In production this would fail a security audit. Always scope to the exact buckets and exact actions needed.
**Mistake 2: Forgetting /* on S3 resource ARNs for object-level actions**

```
# WRONG — refers to the bucket itself, not objects inside it
"Resource": "arn:aws:s3:::chetan-image-raw-<suffix>"

# CORRECT — refers to all objects inside the bucket
"Resource": "arn:aws:s3:::chetan-image-raw-<suffix>/*"
```

S3 IAM has a split between bucket-level actions (s3:ListBucket, s3:GetBucketLocation) which target the bare bucket ARN, and object-level actions (s3:GetObject, s3:PutObject) which target the bucket/* ARN. Getting this wrong gives you 403 AccessDenied that looks identical to "the role doesn't exist" — very confusing to debug.
**Mistake 3: Forgetting the CloudWatch Logs permissions**

Lambda will appear to run (the function returns successfully, no errors in the trigger) but you'll have zero logs anywhere. When you try to debug something later, you're flying blind. Always include these three log actions — they cost nothing and save hours of debugging.
**Mistake 4: Confusing inline policy vs managed policy**

Inline policy — lives inside the role, deleted when the role is deleted, not reusable across roles, visible only on the role detail page. Best for role-specific permissions (like this project).
Managed policy — standalone IAM object with its own ARN, can be attached to many roles, versioned, independently manageable. Best for shared permission sets (like a ReadOnlyS3Policy used by 10 different Lambda functions).

Neither is always better — use inline when the permissions are specific to one role, use managed policies when you'd otherwise copy-paste the same JSON into multiple roles.
**Mistake 5: Confusing the trust policy with the permissions policy**

The --assume-role-policy-document flag in create-role takes the trust policy (who can assume). The put-role-policy command takes the permissions policy (what it can do). Mixing these up is the #1 CLI mistake at this step — AWS CLI error messages for getting this backwards are not always obvious.

Quick Reference Summary

```
aws iam create-role
  └─ attaches TRUST POLICY  (who can assume: lambda.amazonaws.com)

aws iam put-role-policy
  └─ attaches PERMISSIONS POLICY (what can be done: S3 read/write + CloudWatch Logs)

Both together = a complete, functional Lambda execution role
```

### Step 5 — Create the Lambda function

**What You're Actually Building**
Before touching any console or CLI, understand the anatomy of what a Lambda function actually is:

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Lambda Function Object                          │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐             │
│  │   Code       │  │  Config      │  │   Layers     │             │
│  │              │  │              │  │              │             │
│  │ function.zip │  │ Runtime      │  │ pillow-layer │             │
│  │ lambda_      │  │ Handler      │  │ (Pillow +    │             │
│  │ function.py  │  │ Memory 512MB │  │  deps)       │             │
│  │              │  │ Timeout 30s  │  │              │             │
│  │              │  │ Env vars     │  │              │             │
│  └──────────────┘  └──────────────┘  └──────────────┘             │
│                                                                     │
│  Execution Role: image-processor-role  (from Step 4)               │
└─────────────────────────────────────────────────────────────────────┘
```

Lambda stores your code as a zip in an internal S3 bucket (managed by AWS, not yours). When invoked, it extracts that zip into a read-only filesystem at /var/task/ inside a Firecracker microVM. The layer zip is extracted into /opt/ — that's why from PIL import Image works inside Lambda even though Pillow isn't in your function zip: Python's module path includes /opt/python/ by convention.

**5A — Package the Function Code**
On your EC2 instance:

```
cd ~/image-processor

# Confirm your function code is there
ls -la
# should show: lambda_function.py, trust-policy.json, permissions-policy.json

# Create the deployment zip — ONLY the function code, NOT the Pillow deps
# (those live in the layer)
zip function.zip lambda_function.py

# Verify the zip contents — it should contain exactly one file
unzip -l function.zip
```

Expected output:

```
Archive:  function.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
     1842  2026-06-25 10:00   lambda_function.py
---------                     -------
     1842                     1 file
```

If you see any other files (like compiled .pyc files or a __pycache__ directory), that's fine — Lambda will handle them. What you must NOT include in this zip is the pillow-layer/ directory — that belongs in the layer, not the function package.

**5B — Grab your Layer ARN from Step 3**
You need the exact ARN of the Pillow layer you published. If you didn't save it:

```
aws lambda list-layer-versions \
  --layer-name pillow-layer \
  --region us-east-1 \
  --query 'LayerVersions[0].LayerVersionArn' \
  --output text
```
Save it
```
LAYER_ARN=$(aws lambda list-layer-versions \
  --layer-name pillow-layer \
  --region us-east-1 \
  --query 'LayerVersions[0].LayerVersionArn' \
  --output text)
echo $LAYER_ARN
# arn:aws:lambda:us-east-1:<account_id>:layer:pillow-layer:1
```

Also grab your account ID and role ARN:

```
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/image-processor-role"
echo "Account: $ACCOUNT_ID"
echo "Role: $ROLE_ARN"
echo "Layer: $LAYER_ARN"
```

**Method A — GUI
Lambda Console → Create function**
Open Lambda console → click Create function (top right).
**Step 1: Basic information**

```
○ Author from scratch     ← SELECT THIS
○ Use a blueprint
○ Container image
○ Browse serverless app repository
```

Fill in:

**Function name:** image-resize-processor
**Runtime:** Python 3.12
**Architecture:** x86_64 (must match the architecture you used when building the Pillow layer — if you used the standard SAM build image, it built for x86_64)

**Step 2: Execution role**
Expand Change default execution role:

```
○ Create a new role with basic Lambda permissions
○ Use an existing role     ← SELECT THIS
○ Create a new role from AWS Policy templates
```

Under "Existing role" dropdown — select image-processor-role.
Click **Create function**. You land on the function's detail page.

**Step 3: Upload the code**
On the function detail page, you see the Code tab open by default with an inline code editor.
Scroll down to Code source → click Upload from dropdown → select .zip file → click Upload → select function.zip from your local machine (download it from EC2 first using scp or the EC2 console's file transfer if needed) → click Save.
Alternatively, use the inline editor and paste the entire lambda_function.py content directly — the console auto-saves it as a zip internally.
After upload, verify in the inline editor that lambda_function.py appears in the file tree on the left.
Handler field (top of Code source): confirm it shows lambda_function.lambda_handler. If not, click Edit and set it. This tells Lambda which file (lambda_function) and which function inside that file (lambda_handler) to call as the entry point.

**Step 4: Configuration → General settings**
Click the Configuration tab → General configuration → Edit:

Memory: change from 128MB default to 512 MB
Timeout: change from 3s default to 0 min 30 sec
Description: Resizes uploaded images to thumbnail and medium sizes using Pillow

Why 512MB and 30s? Lambda CPU allocation scales linearly with memory. At 128MB you get ~1/8 of a vCPU. At 512MB you get ~1/2 vCPU. Image decode + resize + re-encode on a large JPEG is CPU-bound — the higher memory allocation makes this run 3–4x faster, which means less billed duration. The 30s timeout is a safety ceiling: if your function hangs (corrupted image, network issue on the S3 call), it kills the invocation rather than running to the 15-minute hard limit.
Click Save.

**Step 5: Configuration → Environment variables**
Configuration tab → Environment variables → Edit → Add environment variable:

Key: DEST_BUCKET
Value: chetan-image-processed-<suffix> (your actual processed bucket name)

Click Save.
This is how your Python code reads os.environ['DEST_BUCKET'] — Lambda injects these as real OS environment variables into the execution environment before your handler runs.

**Step 6: Add the Pillow layer**
Still on the function detail page — scroll all the way down past the code editor to the Layers section at the bottom.
Click Add a layer:

```
○ AWS layers
○ Custom layers
○ Specify an ARN     ← SELECT THIS
```

In the ARN field, paste your layer ARN:

```
arn:aws:lambda:us-east-1:<account_id>:layer:pillow-layer:1
```

Click Verify — it should confirm the layer name and compatible runtimes. Click Add.
You should now see "Layers (1)" in the visual diagram at the top of the function page.

**Method B — CLI (single command, all options in one shot)**

```
cd ~/image-processor

SUFFIX="your-suffix-here"

aws lambda create-function \
  --function-name image-resize-processor \
  --runtime python3.12 \
  --role ${ROLE_ARN} \
  --handler lambda_function.lambda_handler \
  --zip-file fileb://function.zip \
  --timeout 30 \
  --memory-size 512 \
  --description "Resizes uploaded images to thumbnail and medium sizes using Pillow" \
  --environment "Variables={DEST_BUCKET=chetan-image-processed-${SUFFIX}}" \
  --layers ${LAYER_ARN} \
  --architectures x86_64 \
  --region us-east-1
```

Expected output (abbreviated):

```
{
    "FunctionName": "image-resize-processor",
    "FunctionArn": "arn:aws:lambda:us-east-1:<account_id>:function:image-resize-processor",
    "Runtime": "python3.12",
    "Role": "arn:aws:iam::<account_id>:role/image-processor-role",
    "Handler": "lambda_function.lambda_handler",
    "CodeSize": 1842,
    "Timeout": 30,
    "MemorySize": 512,
    "State": "Pending",
    "StateReason": "The function is being created.",
    "Layers": [
        {
            "Arn": "arn:aws:lambda:us-east-1:<account_id>:layer:pillow-layer:1",
            "CodeSize": ...
        }
    ]
}
```
The "State": "Pending" is normal — Lambda is packaging everything. It transitions to "Active" within ~10 seconds. Check it:

```
aws lambda get-function \
  --function-name image-resize-processor \
  --query 'Configuration.State' \
  --output text
# Active
```

Save the function ARN:

```
FUNCTION_ARN=$(aws lambda get-function \
  --function-name image-resize-processor \
  --query 'Configuration.FunctionArn' \
  --output text)
echo $FUNCTION_ARN
```

**Verify Step 5 is Complete**

```
aws lambda get-function-configuration \
  --function-name image-resize-processor \
  --region us-east-1 \
  --query '{State:State, Runtime:Runtime, Handler:Handler, MemorySize:MemorySize, Timeout:Timeout, Role:Role, Layers:Layers[*].Arn, EnvVars:Environment.Variables}' \
  --output json
```

Expected:

```
{
    "State": "Active",
    "Runtime": "python3.12",
    "Handler": "lambda_function.lambda_handler",
    "MemorySize": 512,
    "Timeout": 30,
    "Role": "arn:aws:iam::<account_id>:role/image-processor-role",
    "Layers": ["arn:aws:lambda:us-east-1:<account_id>:layer:pillow-layer:1"],
    "EnvVars": {"DEST_BUCKET": "chetan-image-processed-<suffix>"}
}
```

All six fields must look exactly right before proceeding. If anything is off, fix it now

```
# Fix environment variable
aws lambda update-function-configuration \
  --function-name image-resize-processor \
  --environment "Variables={DEST_BUCKET=chetan-image-processed-${SUFFIX}}"

# Fix memory/timeout
aws lambda update-function-configuration \
  --function-name image-resize-processor \
  --timeout 30 \
  --memory-size 512
```


### Step 6 — Wire the S3 trigger (the resource policy + notification config)
This is the step interviewers love to probe — two separate permission objects, both required.
This is the most conceptually important step and the most common failure point. You need to set up two completely separate permission objects — and they do completely different things.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    THE TWO-PERMISSION PROBLEM                               │
│                                                                             │
│  PERMISSION 1: Resource-based policy ON the Lambda function                 │
│  Question answered: "Is S3 service ALLOWED to INVOKE this function?"        │
│  Where it lives: Attached to the Lambda function itself                     │
│  CLI command: aws lambda add-permission                                     │
│                                                                             │
│  PERMISSION 2: S3 Bucket notification configuration                         │
│  Question answered: "WHEN should S3 ACTUALLY invoke the function?"          │
│  Where it lives: Attached to the source S3 bucket                           │
│  CLI command: aws s3api put-bucket-notification-configuration               │
│                                                                             │
│  If only #1: S3 is allowed to invoke but never told to                      │
│  If only #2: S3 tries to invoke but gets Access Denied — silent failure     │
│  Both required: S3 detects upload event → checks #2 → calls Lambda         │
│                 Lambda checks #1 → allows execution → your code runs        │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Step 6A — Resource-Based Policy (Permission for S3 to invoke Lambda)**
**What this is**: A policy document attached to the Lambda function (not to an IAM role) that says "this external entity is allowed to invoke me." This is different from the IAM execution role — the execution role controls what Lambda can do; the resource-based policy controls who can trigger Lambda.
**Why source-account matters**: The --source-account flag prevents what's called the "confused deputy" problem. Without it, any S3 bucket in any AWS account in the world that somehow knew your function ARN could invoke it. With source-account set to your account ID, S3 invocations from buckets in other accounts are rejected even if they have your function ARN. Always include this.
**Method A — GUI:**
The GUI method for 6A and 6B happens together — when you add a trigger via the Lambda console or S3 console, AWS runs the add-permission call automatically behind the scenes. This is why the GUI is deceptively easy — it hides the two-step complexity. We'll cover the GUI path under 6B.
**Method B — CLI:**

```
aws lambda add-permission \
  --function-name image-resize-processor \
  --statement-id AllowS3InvokeFromRawBucket \
  --action lambda:InvokeFunction \
  --principal s3.amazonaws.com \
  --source-arn arn:aws:s3:::chetan-image-raw-${SUFFIX} \
  --source-account ${ACCOUNT_ID} \
  --region us-east-1
```

Flag-by-flag breakdown:

<img width="867" height="432" alt="image" src="https://github.com/user-attachments/assets/15ffc7f9-14f9-4795-be14-d20d635e9a8c" />

Expected output:

```
{
    "Statement": "{\"Sid\":\"AllowS3InvokeFromRawBucket\",\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"s3.amazonaws.com\"},\"Action\":\"lambda:InvokeFunction\",\"Resource\":\"arn:aws:lambda:us-east-1:<account_id>:function:image-resize-processor\",\"Condition\":{\"StringEquals\":{\"AWS:SourceAccount\":\"<account_id>\"},\"ArnLike\":{\"AWS:SourceArn\":\"arn:aws:s3:::chetan-image-raw-<suffix>\"}}}"
}
```

Verify the policy was attached:

```
aws lambda get-policy \
  --function-name image-resize-processor \
  --region us-east-1 \
  --query 'Policy' \
  --output text | python3 -m json.tool
```

**Step 6B — S3 Bucket Notification Configuration**
**What this is:** A configuration object stored as metadata on the S3 bucket that defines event routing rules. It has nothing to do with IAM — it's purely "when event X happens matching filter Y, send to destination Z."
**The infinite loop risk:** If you forget to set the prefix filter to uploads/, any object created ANYWHERE in the bucket (including the processed outputs if they went to the same bucket) would trigger Lambda. That Lambda run would write more files, which would trigger Lambda again, infinitely. We prevent this three ways: (1) prefix filter on the trigger so only uploads/ objects fire the notification, (2) separate destination bucket (our design), and (3) the guard clause in the Python code (if src_key.startswith(('thumbnail/', 'medium/')): continue).
**Create the notification config file:**

```
cat > notification.json << EOF
{
  "LambdaFunctionConfigurations": [
    {
      "Id": "image-resize-trigger",
      "LambdaFunctionArn": "${FUNCTION_ARN}",
      "Events": ["s3:ObjectCreated:*"],
      "Filter": {
        "Key": {
          "FilterRules": [
            {
              "Name": "prefix",
              "Value": "uploads/"
            },
            {
              "Name": "suffix",
              "Value": ".jpg"
            }
          ]
        }
      }
    }
  ]
}
EOF

# Verify substitution happened correctly
cat notification.json
# The LambdaFunctionArn field should show your actual ARN, not ${FUNCTION_ARN}
```

The suffix filter .jpg is optional but good practice — it prevents Lambda triggering on .txt or .json files someone accidentally uploads to uploads/. You can add multiple configurations if you want to handle .png and .jpeg as well (or remove the suffix filter entirely to handle all file types and let the Python code handle format validation with a try/except).
**Method A — GUI:**
Two equivalent paths in the GUI:
**Path 1 — From the Lambda console:**

Lambda Console → image-resize-processor → Configuration tab → Triggers → Add trigger

Trigger configuration: select S3
Bucket: select chetan-image-raw-<suffix> from dropdown
Event types: All object create events
Prefix: uploads/
Suffix: .jpg
Acknowledge the recursive invocation warning checkbox → Add

AWS console automatically runs lambda:add-permission in the background at this point.
**Path 2 — From the S3 console:**

S3 Console → chetan-image-raw-<suffix> → Properties tab → scroll to Event notifications section → **Create event notification**

Event name: image-resize-trigger
Prefix: uploads/
Suffix: .jpg
Event types: check All object create events
Destination: select Lambda function
Lambda function: select image-resize-processor from dropdown
Save changes

**Method B — CLI:**

```
aws s3api put-bucket-notification-configuration \
  --bucket chetan-image-raw-${SUFFIX} \
  --notification-configuration file://notification.json \
  --region us-east-1
```

No output on success. Verify immediately:

```
aws s3api get-bucket-notification-configuration \
  --bucket chetan-image-raw-${SUFFIX} \
  --region us-east-1
```

Expected:

```
{
    "LambdaFunctionConfigurations": [
        {
            "Id": "image-resize-trigger",
            "LambdaFunctionArn": "arn:aws:lambda:us-east-1:<account_id>:function:image-resize-processor",
            "Events": ["s3:ObjectCreated:*"],
            "Filter": {
                "Key": {
                    "FilterRules": [
                        {"Name": "Prefix", "Value": "uploads/"},
                        {"Name": "Suffix", "Value": ".jpg"}
                    ]
                }
            }
        }
    ]
}
```

**Step 6 Complete — Verify the Full Trigger Chain**
The full event chain is now wired:

```
S3 raw bucket (notification config)
   └─→ sends event to Lambda function
          └─→ Lambda resource policy ALLOWS s3.amazonaws.com to invoke
                 └─→ Lambda execution role ALLOWS code to read raw / write processed
```

Confirm all three exist:

```
# 1. Notification config on bucket
aws s3api get-bucket-notification-configuration \
  --bucket chetan-image-raw-${SUFFIX} \
  --query 'LambdaFunctionConfigurations[0].Id' \
  --output text
# image-resize-trigger

# 2. Resource policy on Lambda
aws lambda get-policy \
  --function-name image-resize-processor \
  --query 'Policy' \
  --output text | python3 -c "import sys,json; p=json.load(sys.stdin); print(p['Statement'][0]['Sid'])"
# AllowS3InvokeFromRawBucket

# 3. IAM role attached to function
aws lambda get-function-configuration \
  --function-name image-resize-processor \
  --query 'Role' \
  --output text
# arn:aws:iam::<account_id>:role/image-processor-role
```

All three must return correct values. If any is missing, the trigger will silently fail.

### Step 7 —  Test the Full Pipeline

Testing has four phases: a manual invocation test, a real S3 trigger test, log verification, and output validation.

**7A — Manual Lambda Invocation Test (Before S3 wiring test)**
This tests your Lambda code and IAM permissions in isolation — before involving S3 events. You simulate exactly the event payload that S3 would send.
**First, upload a test image to the raw bucket:**

```
# If you don't have a test image, create a simple one using Python
python3 -c "
from PIL import Image
img = Image.new('RGB', (2000, 1500), color=(73, 109, 137))
img.save('/tmp/test-image.jpg', 'JPEG')
print('Created test-image.jpg: 2000x1500 RGB JPEG')
"

aws s3 cp /tmp/test-image.jpg \
  s3://chetan-image-raw-${SUFFIX}/uploads/test-image.jpg
```

**Create the test event payload:**

```
cat > test-event.json << EOF
{
  "Records": [
    {
      "s3": {
        "bucket": {
          "name": "chetan-image-raw-${SUFFIX}"
        },
        "object": {
          "key": "uploads/test-image.jpg",
          "size": 204800
        }
      }
    }
  ]
}
EOF
```

**Invoke Lambda directly with this payload:**

```
aws lambda invoke \
  --function-name image-resize-processor \
  --payload file://test-event.json \
  --cli-binary-format raw-in-base64-out \
  --log-type Tail \
  --region us-east-1 \
  response.json \
  | python3 -c "
import sys, json, base64
result = json.load(sys.stdin)
print('=== STATUS CODE ===')
print(result.get('StatusCode'))
print()
print('=== FUNCTION LOGS ===')
if 'LogResult' in result:
    print(base64.b64decode(result['LogResult']).decode())
"
```

**What --log-type Tail does:** Lambda captures the last 4KB of log output and returns it base64-encoded in the response. This lets you see your print() statements without opening CloudWatch — critical for quick debugging.
Expected log output:

```
START RequestId: abc-123 Version: $LATEST
Wrote thumbnail/test-image.jpg (150, 113) to chetan-image-processed-<suffix>
Wrote medium/test-image.jpg (800, 600) to chetan-image-processed-<suffix>
END RequestId: abc-123
REPORT RequestId: abc-123  Duration: 1247.32 ms  Billed Duration: 1248 ms
Memory Size: 512 MB  Max Memory Used: 89 MB  Init Duration: 892.14 ms
```

Check the response file:

```
cat response.json
# {"statusCode": 200, "body": "OK"}
```

If you see errors instead, common causes:

```
# AccessDenied on GetObject → execution role missing s3:GetObject
# AccessDenied on PutObject → execution role missing s3:PutObject on processed bucket
# Runtime.ImportModuleError → Pillow layer not attached correctly
# Task timed out → increase timeout or check S3 connectivity
```

**GUI equivalent of manual invoke:**
Lambda Console → image-resize-processor → Test tab → Create new event → Event name: s3-upload-test → paste the test event JSON → Save → Test button.
Results appear inline with logs shown directly in the console. Green = success, red = error with full traceback shown.

**7B — Real S3 Trigger Test**
This tests the full end-to-end pipeline: S3 upload → event notification → Lambda invoke → processed output.

```
# Upload a different image to verify the trigger fires (not just the manual invoke)
aws s3 cp /tmp/test-image.jpg \
  s3://chetan-image-raw-${SUFFIX}/uploads/trigger-test.jpg

echo "Uploaded. Waiting 5 seconds for async Lambda invocation..."
sleep 5

# Check if the processed outputs appeared
echo "=== Checking processed bucket ==="
aws s3 ls s3://chetan-image-processed-${SUFFIX}/ --recursive
```

Expected output:

```
2026-06-25 10:30:15      8432 medium/trigger-test.jpg
2026-06-25 10:30:15      1821 thumbnail/trigger-test.jpg
```

If the files don't appear after 5 seconds, wait a few more — async Lambda invocations from S3 can take up to ~15 seconds in rare cold-start scenarios.

**7C — CloudWatch Logs Verification
CLI — Stream logs live:**

```
# Follow logs in real time — upload another file while this is running
aws logs tail /aws/lambda/image-resize-processor \
  --follow \
  --format short \
  --region us-east-1
```
OUTPUT Format:
```
2026-06-25T10:30:10 START RequestId: xyz-456
2026-06-25T10:30:11 Wrote thumbnail/trigger-test.jpg (150, 113) to chetan-image-processed-...
2026-06-25T10:30:11 Wrote medium/trigger-test.jpg (800, 600) to chetan-image-processed-...
2026-06-25T10:30:11 END RequestId: xyz-456
2026-06-25T10:30:11 REPORT RequestId: xyz-456 Duration: 1247.32 ms Billed Duration: 1248 ms Memory Size: 512 MB Max Memory Used: 89 MB Init Duration: 892.14 ms
```

Note the Init Duration line — this only appears on cold starts (first invocation, or after the execution environment has been idle and recycled). Subsequent rapid invocations won't show this line because the warm container is reused.
Press Ctrl+C to stop following.
**CLI — Query specific log events:**

```
# Get log stream names for this function
aws logs describe-log-streams \
  --log-group-name /aws/lambda/image-resize-processor \
  --order-by LastEventTime \
  --descending \
  --max-items 3 \
  --region us-east-1 \
  --query 'logStreams[*].logStreamName' \
  --output table
```

**GUI**:
CloudWatch Console → Log groups → /aws/lambda/image-resize-processor → click the most recent Log stream → browse individual invocation logs.
Or: Lambda Console → image-resize-processor → Monitor tab → View CloudWatch logs button (takes you directly to the log group).

**7D — Validate the Output Files**

```
# Download the thumbnail and verify dimensions
aws s3 cp \
  s3://chetan-image-processed-${SUFFIX}/thumbnail/trigger-test.jpg \
  /tmp/thumbnail-result.jpg

aws s3 cp \
  s3://chetan-image-processed-${SUFFIX}/medium/trigger-test.jpg \
  /tmp/medium-result.jpg

python3 -c "
from PIL import Image
import os

for path, label in [('/tmp/thumbnail-result.jpg', 'thumbnail'), ('/tmp/medium-result.jpg', 'medium')]:
    with Image.open(path) as img:
        size_kb = os.path.getsize(path) / 1024
        print(f'{label}: {img.size[0]}x{img.size[1]}px, {size_kb:.1f}KB, mode={img.mode}')
"
```

Expected output:

```
thumbnail: 150x113px, 3.2KB, mode=RGB
medium: 800x600px, 42.7KB, mode=RGB
```

The dimensions preserve aspect ratio — thumbnail() in Pillow scales within the bounding box while maintaining the original proportions. A 2000x1500 image fits within 150x150 as 150x113 (not 150x150, which would be a crop/distort).
Compare to the original:

```
python3 -c "
from PIL import Image
import os

with Image.open('/tmp/test-image.jpg') as img:
    size_kb = os.path.getsize('/tmp/test-image.jpg') / 1024
    print(f'original: {img.size[0]}x{img.size[1]}px, {size_kb:.1f}KB')
"
```

Output:

```
original: 2000x1500px, 89.3KB, mode=RGB
```

The size reduction confirms Pillow's JPEG quality=80 compression is working alongside the pixel resize. In real-world scenarios (high-resolution photos from phones), you'd typically see original files of 3–8MB shrinking to thumbnails of 5–15KB — 200–500x compression ratios.

**7E — Test Edge Cases
Test 1: Non-image file (poison pill)**

```
echo "not an image" | aws s3 cp - \
  s3://chetan-image-raw-${SUFFIX}/uploads/bad-file.jpg

sleep 5

# Check Lambda logs for the error
aws logs tail /aws/lambda/image-resize-processor \
  --since 1m \
  --format short \
  --region us-east-1
```

You'll see an error logged and Lambda will retry 2 more times (S3 async invocation default). The exception is caught, logged, and re-raised — the raise at the end of the except block tells Lambda the invocation failed, triggering the retry logic.
**Test 2: Image outside uploads/ prefix (should NOT trigger)**

```
# This upload goes to root of bucket, not uploads/ prefix
aws s3 cp /tmp/test-image.jpg \
  s3://chetan-image-raw-${SUFFIX}/no-trigger-test.jpg

sleep 5

# Verify nothing new appeared in processed bucket
aws s3 ls s3://chetan-image-processed-${SUFFIX}/ --recursive
# Should still show only the files from earlier tests
```

**Test 3: Concurrent uploads (Lambda scaling)**

```
# Upload 5 images simultaneously
for i in {1..5}; do
  aws s3 cp /tmp/test-image.jpg \
    s3://chetan-image-raw-${SUFFIX}/uploads/concurrent-test-${i}.jpg &
done
wait
echo "All 5 uploads complete"

sleep 10

# Count the results — should see 10 files (5 thumbnails + 5 mediums)
aws s3 ls s3://chetan-image-processed-${SUFFIX}/ --recursive | wc -l
```

Lambda scales out to handle concurrent invocations — each upload may be processed by a separate Lambda execution environment running in parallel. This is the scale-out model in action.

**Full End-to-End Verification Checklist**

```
echo "=== Step 5: Lambda Function ==="
aws lambda get-function-configuration \
  --function-name image-resize-processor \
  --query '{State:State,Memory:MemorySize,Timeout:Timeout,Layers:length(Layers)}' \
  --output json

echo ""
echo "=== Step 6A: Resource Policy ==="
aws lambda get-policy \
  --function-name image-resize-processor \
  --query 'Policy' \
  --output text | python3 -c "
import sys,json
p=json.loads(sys.stdin.read())
for s in p['Statement']:
    print(f'  Sid: {s[\"Sid\"]}')
    print(f'  Principal: {s[\"Principal\"]}')
    print(f'  Action: {s[\"Action\"]}')
"

echo ""
echo "=== Step 6B: Bucket Notification ==="
aws s3api get-bucket-notification-configuration \
  --bucket chetan-image-raw-${SUFFIX} \
  --query 'LambdaFunctionConfigurations[0].{Id:Id,Events:Events,Prefix:Filter.Key.FilterRules[0].Value}' \
  --output json

echo ""
echo "=== Step 7: Processed Output ==="
aws s3 ls s3://chetan-image-processed-${SUFFIX}/ --recursive \
  | awk '{print $3, $4}' \
  | sort
```

### Step 8 — Cleanup (avoid surprise charges)

```
aws lambda remove-permission --function-name image-resize-processor --statement-id s3invoke
aws lambda delete-function --function-name image-resize-processor
aws lambda delete-layer-version --layer-name pillow-layer --version-number 1
aws iam delete-role-policy --role-name image-processor-role --policy-name image-processor-permissions
aws iam delete-role --role-name image-processor-role
aws s3 rb s3://chetan-image-raw-<suffix> --force
aws s3 rb s3://chetan-image-processed-<suffix> --force
```

**Common Failure Modes at Steps 5/6/7 and How to Debug Them**

<img width="1277" height="417" alt="image" src="https://github.com/user-attachments/assets/343e452c-c3ee-4e24-93bf-cf4032d6ca96" />
