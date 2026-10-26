"""IDrive e2 calls. Interactive transfers are browser-direct; preview workers use bounded temporary files."""
from urllib.parse import urlparse, quote

import frappe


def config():
    from botocore.config import Config
    import boto3
    conf = frappe.conf
    keys = ['teaching_resources_endpoint', 'teaching_resources_bucket', 'teaching_resources_access_key', 'teaching_resources_secret_key']
    if any(not conf.get(k) for k in keys):
        frappe.throw('Teaching resource storage is not configured. Contact School Admin.')
    endpoint = conf.get(keys[0])
    parsed = urlparse(endpoint)
    if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.idrivee2.com') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        frappe.throw('Use the HTTPS IDrive e2 regional endpoint without a bucket path.')
    client = boto3.client('s3', endpoint_url=endpoint, region_name=conf.get('teaching_resources_region') or 'ap-southeast-1', aws_access_key_id=conf.get(keys[2]), aws_secret_access_key=conf.get(keys[3]), config=Config(signature_version='s3v4', connect_timeout=10, read_timeout=30, retries={'max_attempts': 2}, s3={'addressing_style': 'path'}))
    return client, conf.get(keys[1])


def start(key, mime, marker):
    s3, bucket = config()
    return s3.create_multipart_upload(Bucket=bucket, Key=key, ContentType=mime, Metadata={'qas-file': marker})['UploadId']


def parts(key, upload_id):
    s3, bucket = config()
    rows = []
    for page in s3.get_paginator('list_parts').paginate(Bucket=bucket, Key=key, UploadId=upload_id):
        rows.extend(page.get('Parts', []))
    return sorted(rows, key=lambda row: row['PartNumber'])


def part_url(key, upload_id, number):
    s3, bucket = config()
    return s3.generate_presigned_url('upload_part', Params={'Bucket': bucket, 'Key': key, 'UploadId': upload_id, 'PartNumber': number}, ExpiresIn=900)


def finish(key, upload_id, rows):
    s3, bucket = config()
    s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={'Parts': [{'PartNumber': r['PartNumber'], 'ETag': r['ETag']} for r in rows]})


def head(key):
    s3, bucket = config()
    return s3.head_object(Bucket=bucket, Key=key)


def absent(error, *codes):
    return getattr(error, 'response', {}).get('Error', {}).get('Code') in codes


def abort(key, upload_id):
    s3, bucket = config()
    try:
        s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
    except Exception as exc:
        if not absent(exc, 'NoSuchUpload'):
            raise


def delete(key):
    s3, bucket = config()
    s3.delete_object(Bucket=bucket, Key=key)


def get_url(key, filename, inline, lifetime):
    s3, bucket = config()
    disposition = 'inline' if inline else 'attachment'
    return s3.generate_presigned_url('get_object', Params={'Bucket': bucket, 'Key': key, 'ResponseContentDisposition': disposition + "; filename*=UTF-8''" + quote(filename, safe=''), 'ResponseCacheControl': 'private, no-store'}, ExpiresIn=lifetime)


def download_preview_source(key, path, maximum):
    s3, bucket = config()
    response = s3.get_object(Bucket=bucket, Key=key)
    body = response['Body']
    try:
        if int(response['ContentLength']) > maximum:
            raise ValueError('Preview input exceeds size limit.')
        total = 0
        with open(path, 'wb') as stream:
            while True:
                chunk = body.read(min(1024 * 1024, maximum - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise ValueError('Preview input exceeds size limit.')
                stream.write(chunk)
        return total
    finally:
        body.close()


def upload_preview(key, path, source_file):
    s3, bucket = config()
    s3.upload_file(str(path), bucket, key, ExtraArgs={'ContentType': 'application/pdf', 'Metadata': {'qas-preview-of': source_file}})
