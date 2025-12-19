# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for binary transparency verification."""

import pretend
import pytest
import requests

from twine import exceptions
from twine import repository
from twine import utils

# --- URL Construction Tests ---


def test_transparency_url_from_pypi():
    """Strip /legacy/ and construct transparency URL for PyPI."""
    repo = repository.Repository(
        repository_url="https://upload.pypi.org/legacy/",
        username="u",
        password="p",
        transparency_enabled=True,
    )
    package = pretend.stub(
        safe_name="mypackage",
        version="1.0.0",
        basefilename="mypackage-1.0.0.tar.gz",
    )

    url = repo._get_transparency_url(package)
    assert url == (
        "https://upload.pypi.org/transparency/mypackage/1.0.0/"
        "mypackage-1.0.0.tar.gz/info"
    )


def test_transparency_url_from_testpypi():
    """Handle TestPyPI URL."""
    repo = repository.Repository(
        repository_url="https://test.pypi.org/legacy/",
        username="u",
        password="p",
        transparency_enabled=True,
    )
    package = pretend.stub(
        safe_name="mypackage",
        version="2.0.0",
        basefilename="mypackage-2.0.0-py3-none-any.whl",
    )

    url = repo._get_transparency_url(package)
    assert url == (
        "https://test.pypi.org/transparency/mypackage/2.0.0/"
        "mypackage-2.0.0-py3-none-any.whl/info"
    )


def test_transparency_url_no_legacy_suffix():
    """Handle URL without /legacy/ suffix."""
    repo = repository.Repository(
        repository_url="https://custom.pypi.example.com/",
        username="u",
        password="p",
        transparency_enabled=True,
    )
    package = pretend.stub(
        safe_name="mypackage",
        version="1.0.0",
        basefilename="mypackage-1.0.0.tar.gz",
    )

    url = repo._get_transparency_url(package)
    assert url == (
        "https://custom.pypi.example.com/transparency/mypackage/1.0.0/"
        "mypackage-1.0.0.tar.gz/info"
    )


# --- Verification Logic Tests ---


def test_verify_transparency_checksum_matches():
    """Pass when checksums match."""
    repo = repository.Repository(
        repository_url=utils.DEFAULT_REPOSITORY,
        username="u",
        password="p",
        transparency_enabled=True,
    )

    package = pretend.stub(
        safe_name="mypackage",
        version="1.0.0",
        basefilename="mypackage-1.0.0.tar.gz",
        sha2_digest="abc123def456",
    )

    # Mock the session.get to return matching transparency data
    transparency_response = {
        "version": 1,
        "log_origin": "bt-log.pypi.org",
        "entry_index": 12345,
        "entry": {
            "checksum": "sha256:abc123def456",
            "filename": "mypackage-1.0.0.tar.gz",
        },
        "checkpoint": "base64checkpoint",
        "inclusion_proof": ["hash1", "hash2"],
    }

    repo.session = pretend.stub(
        get=lambda url, headers: pretend.stub(
            status_code=200,
            json=lambda: transparency_response,
        )
    )

    # Should not raise
    repo.verify_package_integrity(package)


def test_verify_transparency_checksum_mismatch():
    """Raise TransparencyVerificationError on checksum mismatch."""
    repo = repository.Repository(
        repository_url=utils.DEFAULT_REPOSITORY,
        username="u",
        password="p",
        transparency_enabled=True,
    )

    package = pretend.stub(
        safe_name="mypackage",
        version="1.0.0",
        basefilename="mypackage-1.0.0.tar.gz",
        sha2_digest="abc123def456",
    )

    # Mock response with DIFFERENT checksum
    transparency_response = {
        "version": 1,
        "log_origin": "bt-log.pypi.org",
        "entry_index": 12345,
        "entry": {
            "checksum": "sha256:DIFFERENT_CHECKSUM",
            "filename": "mypackage-1.0.0.tar.gz",
        },
        "checkpoint": "base64checkpoint",
        "inclusion_proof": ["hash1", "hash2"],
    }

    repo.session = pretend.stub(
        get=lambda url, headers: pretend.stub(
            status_code=200,
            json=lambda: transparency_response,
        )
    )

    with pytest.raises(
        exceptions.TransparencyVerificationError, match="checksum mismatch"
    ):
        repo.verify_package_integrity(package)


def test_verify_transparency_filename_mismatch():
    """Raise TransparencyVerificationError on filename mismatch."""
    repo = repository.Repository(
        repository_url=utils.DEFAULT_REPOSITORY,
        username="u",
        password="p",
        transparency_enabled=True,
    )

    package = pretend.stub(
        safe_name="mypackage",
        version="1.0.0",
        basefilename="mypackage-1.0.0.tar.gz",
        sha2_digest="abc123def456",
    )

    # Mock response with DIFFERENT filename
    transparency_response = {
        "version": 1,
        "log_origin": "bt-log.pypi.org",
        "entry_index": 12345,
        "entry": {
            "checksum": "sha256:abc123def456",
            "filename": "DIFFERENT_FILENAME.tar.gz",
        },
        "checkpoint": "base64checkpoint",
        "inclusion_proof": ["hash1", "hash2"],
    }

    repo.session = pretend.stub(
        get=lambda url, headers: pretend.stub(
            status_code=200,
            json=lambda: transparency_response,
        )
    )

    with pytest.raises(
        exceptions.TransparencyVerificationError, match="filename mismatch"
    ):
        repo.verify_package_integrity(package)


# --- HTTP Interaction Tests ---


def test_fetch_transparency_info_success():
    """Fetch and return transparency data on 200."""
    repo = repository.Repository(
        repository_url=utils.DEFAULT_REPOSITORY,
        username="u",
        password="p",
        transparency_enabled=True,
    )

    expected_data = {
        "version": 1,
        "log_origin": "bt-log.pypi.org",
        "entry_index": 12345,
        "entry": {
            "checksum": "sha256:abc123",
            "filename": "pkg-1.0.0.tar.gz",
        },
        "checkpoint": "base64checkpoint",
        "inclusion_proof": ["hash1"],
    }

    repo.session = pretend.stub(
        get=lambda url, headers: pretend.stub(
            status_code=200,
            json=lambda: expected_data,
        )
    )

    package = pretend.stub(
        safe_name="pkg",
        version="1.0.0",
        basefilename="pkg-1.0.0.tar.gz",
    )

    result = repo._fetch_transparency_info(package)
    assert result == expected_data


def test_fetch_transparency_info_404():
    """Raise TransparencyVerificationError on 404."""
    repo = repository.Repository(
        repository_url=utils.DEFAULT_REPOSITORY,
        username="u",
        password="p",
        transparency_enabled=True,
    )

    repo.session = pretend.stub(get=lambda url, headers: pretend.stub(status_code=404))

    package = pretend.stub(
        safe_name="pkg",
        version="1.0.0",
        basefilename="pkg-1.0.0.tar.gz",
    )

    with pytest.raises(exceptions.TransparencyVerificationError, match="HTTP 404"):
        repo._fetch_transparency_info(package)


def test_fetch_transparency_info_500():
    """Raise TransparencyVerificationError on server error."""
    repo = repository.Repository(
        repository_url=utils.DEFAULT_REPOSITORY,
        username="u",
        password="p",
        transparency_enabled=True,
    )

    repo.session = pretend.stub(get=lambda url, headers: pretend.stub(status_code=500))

    package = pretend.stub(
        safe_name="pkg",
        version="1.0.0",
        basefilename="pkg-1.0.0.tar.gz",
    )

    with pytest.raises(exceptions.TransparencyVerificationError, match="HTTP 500"):
        repo._fetch_transparency_info(package)


def test_fetch_transparency_info_network_error():
    """Raise TransparencyVerificationError on network error."""
    repo = repository.Repository(
        repository_url=utils.DEFAULT_REPOSITORY,
        username="u",
        password="p",
        transparency_enabled=True,
    )

    def raise_connection_error(url, headers):
        raise requests.ConnectionError("Network unreachable")

    repo.session = pretend.stub(get=raise_connection_error)

    package = pretend.stub(
        safe_name="pkg",
        version="1.0.0",
        basefilename="pkg-1.0.0.tar.gz",
    )

    with pytest.raises(exceptions.TransparencyVerificationError, match="Network"):
        repo._fetch_transparency_info(package)


# --- Disabled By Default Tests ---


def test_verify_package_integrity_disabled_by_default():
    """Skip verification when transparency_enabled is False (default)."""
    repo = repository.Repository(
        repository_url=utils.DEFAULT_REPOSITORY,
        username="u",
        password="p",
        # transparency_enabled defaults to False
    )

    package = pretend.stub(basefilename="fake.whl")

    # Should not raise, should do nothing (no HTTP call)
    # If it tried to make an HTTP call, this would fail since session is real
    repo.verify_package_integrity(package)


def test_verify_package_integrity_explicitly_disabled():
    """Skip verification when transparency_enabled is explicitly False."""
    repo = repository.Repository(
        repository_url=utils.DEFAULT_REPOSITORY,
        username="u",
        password="p",
        transparency_enabled=False,
    )

    package = pretend.stub(basefilename="fake.whl")

    # Should not raise
    repo.verify_package_integrity(package)
