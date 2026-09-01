import json

from minio.error import S3Error

from yuxi.storage.minio.client import MinIOClient, normalize_public_minio_url


class FakeMinio:
    def __init__(self):
        self.policy = None

    def bucket_exists(self, bucket_name: str) -> bool:
        return False

    def make_bucket(self, bucket_name: str) -> None:
        return None

    def set_bucket_policy(self, bucket_name: str, policy: str) -> None:
        self.policy = json.loads(policy)

    def put_object(self, **kwargs):
        return object()


def test_public_image_uses_same_origin_url_without_bucket_listing(monkeypatch):
    monkeypatch.setenv("MINIO_PUBLIC_URL", "/minio")
    client = MinIOClient()
    fake_minio = FakeMinio()
    client._client = fake_minio

    result = client.upload_file("public", "images/user 1/avatar.png", b"image", "image/png")

    assert result.url == "/minio/public/images/user%201/avatar.png"
    assert fake_minio.policy is not None
    actions = [action for statement in fake_minio.policy["Statement"] for action in statement["Action"]]
    assert actions == ["s3:GetObject"]


def test_legacy_public_minio_url_is_normalized_to_same_origin(monkeypatch):
    monkeypatch.setenv("MINIO_PUBLIC_URL", "/minio")

    assert (
        normalize_public_minio_url("http://example.test:9000/public/avatar/user.png") == "/minio/public/avatar/user.png"
    )
    assert normalize_public_minio_url("https://cdn.example.test/public/user.png") == (
        "https://cdn.example.test/public/user.png"
    )


def test_legacy_public_minio_url_preserves_query_and_fragment(monkeypatch):
    monkeypatch.setenv("MINIO_PUBLIC_URL", "/minio")

    assert (
        normalize_public_minio_url("http://example.test:9000/public/avatar/user.png?v=123#preview")
        == "/minio/public/avatar/user.png?v=123#preview"
    )


def test_existing_bucket_race_is_treated_as_success():
    class RacingMinio(FakeMinio):
        def make_bucket(self, bucket_name: str) -> None:
            raise S3Error(
                None,
                "BucketAlreadyOwnedByYou",
                "The bucket was created by another concurrent request.",
                f"/{bucket_name}",
                "request-id",
                "host-id",
                bucket_name,
            )

    client = MinIOClient()
    client._client = RacingMinio()

    assert client.ensure_bucket_exists("knowledgebases") is True
