"""Chaos #3: S3 bucket misconfiguration.

Flips the menu bucket's policy to public-read (a P1 security incident for the
S3 adapter, which should revert to the stored baseline). --delete-canary
instead removes the canary object so the synthetic fetch fails.
"""

import argparse
import json


def _client():
    import boto3

    from ._common import MINIO_ENDPOINT, MINIO_KEY, MINIO_SECRET

    return boto3.client(
        "s3", endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_KEY, aws_secret_access_key=MINIO_SECRET,
    )


PUBLIC_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow", "Principal": "*",
        "Action": ["s3:GetObject", "s3:ListBucket"],
        "Resource": ["arn:aws:s3:::{bucket}", "arn:aws:s3:::{bucket}/*"],
    }],
}


def main(bucket: str, delete_canary: bool, canary: str) -> None:
    s3 = _client()
    try:
        s3.create_bucket(Bucket=bucket)
        s3.put_object(Bucket=bucket, Key=canary, Body=b"fake-jpeg-bytes",
                      ContentType="image/jpeg")
    except Exception:
        pass  # already seeded
    if delete_canary:
        s3.delete_object(Bucket=bucket, Key=canary)
        print(f"canary object {canary} deleted from {bucket}")
        return
    policy = json.loads(json.dumps(PUBLIC_POLICY).replace("{bucket}", bucket))
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
    print(f"bucket {bucket} flipped to PUBLIC — the s3 adapter should classify this P1 and revert")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", default="menu")
    parser.add_argument("--canary", default="margherita.jpg")
    parser.add_argument("--delete-canary", action="store_true")
    args = parser.parse_args()
    main(args.bucket, args.delete_canary, args.canary)
