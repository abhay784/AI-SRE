"""S3Adapter._is_public — regression coverage for the shapes S3-compatible
stores actually return.

Found live against a real MinIO cluster: a policy created with the
conventional `Principal: "*"` gets normalized by MinIO (and by real S3, once
round-tripped) into `{"AWS": ["*"]}`. The original exact-match check missed
that shape, so a genuinely public bucket was classified P2 instead of P1.
"""

from slopsaver.adapters.s3_adapter import S3Adapter


def _policy(principal):
    return {"Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": principal,
                           "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::x/*"]}]}


def test_bare_star_principal_is_public():
    assert S3Adapter._is_public(_policy("*")) is True


def test_aws_star_string_is_public():
    assert S3Adapter._is_public(_policy({"AWS": "*"})) is True


def test_aws_star_list_is_public():
    # MinIO's actual return shape for a Principal:"*" policy — the case that
    # was originally missed.
    assert S3Adapter._is_public(_policy({"AWS": ["*"]})) is True


def test_scoped_principal_is_not_public():
    assert S3Adapter._is_public(_policy({"AWS": "arn:aws:iam::123456789012:root"})) is False
    assert S3Adapter._is_public(_policy({"AWS": ["arn:aws:iam::123456789012:root"]})) is False


def test_deny_statement_with_star_is_not_public():
    policy = {"Version": "2012-10-17",
              "Statement": [{"Effect": "Deny", "Principal": "*",
                             "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::x/*"]}]}
    assert S3Adapter._is_public(policy) is False


def test_none_policy_is_not_public():
    assert S3Adapter._is_public(None) is False
