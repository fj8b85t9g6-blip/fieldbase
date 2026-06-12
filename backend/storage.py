import os
import boto3
from botocore.config import Config

_ROOT = None
_BUCKET = None
_client = None
USE_R2 = False

_LOCAL_DIRS = {}
_LOCAL_URLS = {
    'photos':   '/static/uploads/photos',
    'docs':     '/static/uploads/docs',
    'receipts': '/uploads/receipts',
}


def init(root: str):
    global _ROOT, _BUCKET, USE_R2, _LOCAL_DIRS
    _ROOT = root
    _BUCKET = os.environ.get('R2_BUCKET_NAME', 'fieldbase')
    USE_R2 = bool(
        os.environ.get('R2_ACCOUNT_ID') and
        os.environ.get('R2_ACCESS_KEY_ID') and
        os.environ.get('R2_SECRET_ACCESS_KEY')
    )
    _LOCAL_DIRS = {
        'photos':   os.path.join(root, 'frontend', 'static', 'uploads', 'photos'),
        'docs':     os.path.join(root, 'frontend', 'static', 'uploads', 'docs'),
        'receipts': os.path.join(root, 'uploads', 'receipts'),
    }
    if not USE_R2:
        for d in _LOCAL_DIRS.values():
            os.makedirs(d, exist_ok=True)


def _s3():
    global _client
    if _client is None:
        account_id = os.environ['R2_ACCOUNT_ID']
        _client = boto3.client(
            's3',
            endpoint_url=f'https://{account_id}.r2.cloudflarestorage.com',
            aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
            aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
            config=Config(signature_version='s3v4'),
            region_name='auto',
        )
    return _client


def upload(file_obj, folder: str, filename: str):
    if USE_R2:
        _s3().upload_fileobj(file_obj, _BUCKET, f'{folder}/{filename}')
    else:
        file_obj.save(os.path.join(_LOCAL_DIRS[folder], filename))


def delete(folder: str, filename: str):
    if USE_R2:
        try:
            _s3().delete_object(Bucket=_BUCKET, Key=f'{folder}/{filename}')
        except Exception:
            pass
    else:
        try:
            os.remove(os.path.join(_LOCAL_DIRS[folder], filename))
        except FileNotFoundError:
            pass


def url(folder: str, filename: str, expires: int = 3600) -> str:
    if USE_R2:
        return _s3().generate_presigned_url(
            'get_object',
            Params={'Bucket': _BUCKET, 'Key': f'{folder}/{filename}'},
            ExpiresIn=expires,
        )
    return f'{_LOCAL_URLS[folder]}/{filename}'
