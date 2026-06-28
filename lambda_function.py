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
