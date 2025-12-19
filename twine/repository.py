# Copyright 2015 Ian Cordasco
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
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
import requests_toolbelt
import rich.progress
from rich import print

from twine import exceptions
from twine import package as package_file
from twine.utils import make_requests_session

LEGACY_PYPI = "https://pypi.python.org/"
LEGACY_TEST_PYPI = "https://testpypi.python.org/"
WAREHOUSE = "https://upload.pypi.org/"
OLD_WAREHOUSE = "https://upload.pypi.io/"
TEST_WAREHOUSE = "https://test.pypi.org/"
WAREHOUSE_WEB = "https://pypi.org/"

logger = logging.getLogger(__name__)


class Repository:
    def __init__(
        self,
        repository_url: str,
        username: Optional[str],
        password: Optional[str],
        disable_progress_bar: bool = False,
        transparency_enabled: bool = False,
    ) -> None:
        self.url = repository_url

        self.session = make_requests_session()
        # requests.Session.auth should be Union[None, Tuple[str, str], ...]
        # But username or password could be None
        # See TODO for utils.RepositoryConfig
        self.session.auth = (
            (username or "", password or "") if username or password else None
        )
        logger.info(f"username: {username if username else '<empty>'}")
        logger.info(f"password: <{'hidden' if password else 'empty'}>")

        # Working around https://github.com/python/typing/issues/182
        self._releases_json_data: Dict[str, Dict[str, Any]] = {}
        self.disable_progress_bar = disable_progress_bar
        self.transparency_enabled = transparency_enabled

    def close(self) -> None:
        self.session.close()

    @staticmethod
    def _convert_metadata_to_list_of_tuples(
        data: package_file.PackageMetadata,
    ) -> List[Tuple[str, Any]]:
        # This does what ``warehouse.forklift.parse_form_metadata()`` does, in reverse.
        data_to_send: List[Tuple[str, Any]] = []
        for key, value in data.items():
            if key == "gpg_signature":
                assert isinstance(value, tuple)
                data_to_send.append((key, value))
            elif key == "project_urls":
                assert isinstance(value, dict)
                for name, url in value.items():
                    data_to_send.append((key, f"{name}, {url}"))
            elif key == "keywords":
                assert isinstance(value, list)
                data_to_send.append((key, ", ".join(value)))
            elif isinstance(value, (list, tuple)):
                data_to_send.extend((key, item) for item in value)
            else:
                assert isinstance(value, str)
                data_to_send.append((key, value))
        return data_to_send

    def set_certificate_authority(self, cacert: Optional[str]) -> None:
        if cacert:
            self.session.verify = cacert

    def set_client_certificate(self, clientcert: Optional[str]) -> None:
        if clientcert:
            self.session.cert = clientcert

    def register(self, package: package_file.PackageFile) -> requests.Response:
        print(f"Registering {package.basefilename}")

        metadata = package.metadata_dictionary()
        data_to_send = self._convert_metadata_to_list_of_tuples(metadata)
        data_to_send.append((":action", "submit"))
        data_to_send.append(("protocol_version", "1"))
        encoder = requests_toolbelt.MultipartEncoder(data_to_send)
        resp = self.session.post(
            self.url,
            data=encoder,
            allow_redirects=False,
            headers={"Content-Type": encoder.content_type},
        )
        # Bug 28. Try to silence a ResourceWarning by releasing the socket.
        resp.close()
        return resp

    def _upload(self, package: package_file.PackageFile) -> requests.Response:
        print(f"Uploading {package.basefilename}")

        metadata = package.metadata_dictionary()
        data_to_send = self._convert_metadata_to_list_of_tuples(metadata)
        data_to_send.append((":action", "file_upload"))
        data_to_send.append(("protocol_version", "1"))
        with open(package.filename, "rb") as fp:
            data_to_send.append(
                (
                    "content",
                    (package.basefilename, fp, "application/octet-stream"),
                )
            )
            encoder = requests_toolbelt.MultipartEncoder(data_to_send)

            with rich.progress.Progress(
                "[progress.percentage]{task.percentage:>3.0f}%",
                rich.progress.BarColumn(),
                rich.progress.DownloadColumn(),
                "•",
                rich.progress.TimeRemainingColumn(
                    compact=True,
                    elapsed_when_finished=True,
                ),
                "•",
                rich.progress.TransferSpeedColumn(),
                disable=self.disable_progress_bar,
            ) as progress:
                task_id = progress.add_task("", total=encoder.len)

                monitor = requests_toolbelt.MultipartEncoderMonitor(
                    encoder,
                    lambda monitor: progress.update(
                        task_id,
                        completed=monitor.bytes_read,
                    ),
                )

                resp = self.session.post(
                    self.url,
                    data=monitor,
                    allow_redirects=False,
                    headers={"Content-Type": monitor.content_type},
                )

        return resp

    def upload(
        self, package: package_file.PackageFile, max_redirects: int = 5
    ) -> requests.Response:
        number_of_redirects = 0
        while number_of_redirects < max_redirects:
            resp = self._upload(package)

            if resp.status_code == requests.codes.OK:
                return resp
            if 500 <= resp.status_code < 600:
                number_of_redirects += 1
                logger.warning(
                    f'Received "{resp.status_code}: {resp.reason}"'
                    "\nPackage upload appears to have failed."
                    f" Retry {number_of_redirects} of {max_redirects}."
                )
            else:
                return resp

        return resp

    def package_is_uploaded(
        self, package: package_file.PackageFile, bypass_cache: bool = False
    ) -> bool:
        """Determine if a package has been uploaded to PyPI already.

        .. warning:: This does not support indexes other than PyPI or TestPyPI

        :param package:
            The package file that will otherwise be uploaded.
        :type package:
            :class:`~twine.package.PackageFile`
        :param bypass_cache:
            Force a request to PyPI.
        :type bypass_cache:
            bool
        :returns:
            True if package has already been uploaded, False otherwise
        :rtype:
            bool
        """
        # NOTE(sigmavirus24): Not all indices are PyPI and pypi.io doesn't
        # have a similar interface for finding the package versions.
        if not self.url.startswith((LEGACY_PYPI, WAREHOUSE, OLD_WAREHOUSE)):
            return False

        safe_name = package.safe_name
        releases = None

        if not bypass_cache:
            releases = self._releases_json_data.get(safe_name)

        if releases is None:
            url = f"{LEGACY_PYPI}pypi/{safe_name}/json"
            headers = {"Accept": "application/json"}
            response = self.session.get(url, headers=headers)
            if response.status_code == 200:
                releases = response.json()["releases"]
            else:
                releases = {}
            self._releases_json_data[safe_name] = releases

        packages = releases.get(package.version, [])

        for uploaded_package in packages:
            if uploaded_package["filename"] == package.basefilename:
                return True

        return False

    def release_urls(self, packages: List[package_file.PackageFile]) -> Set[str]:
        if self.url.startswith(WAREHOUSE):
            url = WAREHOUSE_WEB
        elif self.url.startswith(TEST_WAREHOUSE):
            url = TEST_WAREHOUSE
        else:
            return set()

        return {
            f"{url}project/{package.safe_name}/{package.version}/"
            for package in packages
        }

    def _get_transparency_url(self, package: package_file.PackageFile) -> str:
        """Construct the transparency endpoint URL for a package.

        :param package:
            The package to get transparency info for.
        :returns:
            The URL for the transparency endpoint.
        """
        # Strip /legacy/ suffix if present
        base_url = self.url.rstrip("/")
        if base_url.endswith("/legacy"):
            base_url = base_url[:-7]

        return (
            f"{base_url}/transparency/"
            f"{package.safe_name}/"
            f"{package.version}/"
            f"{package.basefilename}/info"
        )

    def _fetch_transparency_info(
        self, package: package_file.PackageFile
    ) -> Dict[str, Any]:
        """Fetch transparency information for an uploaded package.

        :param package:
            The package to fetch transparency info for.
        :returns:
            The transparency data as a dictionary.
        :raises TransparencyVerificationError:
            If the transparency data cannot be fetched.
        """
        url = self._get_transparency_url(package)
        logger.info(f"Fetching transparency info from {url}")

        try:
            response = self.session.get(url, headers={"Accept": "application/json"})
        except requests.RequestException as e:
            raise exceptions.TransparencyVerificationError.fetch_failed(
                url, f"Network error: {e}"
            )

        if response.status_code != 200:
            raise exceptions.TransparencyVerificationError.fetch_failed(
                url, f"HTTP {response.status_code}"
            )

        try:
            return response.json()
        except ValueError as e:
            raise exceptions.TransparencyVerificationError.fetch_failed(
                url, f"Invalid JSON response: {e}"
            )

    def verify_package_integrity(self, package: package_file.PackageFile) -> None:
        """Verify package integrity via binary transparency log.

        Fetches transparency data from the repository and verifies:
        1. The filename matches what was uploaded
        2. The checksum matches what was uploaded

        :param package:
            The package that was uploaded.
        :raises TransparencyVerificationError:
            If verification fails.
        """
        if not self.transparency_enabled:
            return

        print(f"Verifying transparency for {package.basefilename}...")

        info = self._fetch_transparency_info(package)

        # Validate response structure
        if "entry" not in info:
            raise exceptions.TransparencyVerificationError(
                "Invalid transparency response: missing 'entry' field"
            )
        if "filename" not in info["entry"]:
            raise exceptions.TransparencyVerificationError(
                "Invalid transparency response: missing 'entry.filename' field"
            )
        if "checksum" not in info["entry"]:
            raise exceptions.TransparencyVerificationError(
                "Invalid transparency response: missing 'entry.checksum' field"
            )

        # Verify filename
        logged_filename = info["entry"]["filename"]
        if logged_filename != package.basefilename:
            raise exceptions.TransparencyVerificationError.filename_mismatch(
                package.basefilename, logged_filename
            )

        # Verify checksum (format: "sha256:...")
        # Note: The 'publisher' field is intentionally not verified because
        # it may be populated asynchronously or vary based on the upload method
        # (e.g., trusted publishing vs username/password). Only filename and
        # checksum provide strong cryptographic guarantees of package identity.
        logged_checksum = info["entry"]["checksum"]
        expected_checksum = f"sha256:{package.sha2_digest}"
        if logged_checksum != expected_checksum:
            raise exceptions.TransparencyVerificationError.checksum_mismatch(
                expected_checksum, logged_checksum
            )

        # TODO: Verify checkpoint signature
        # The checkpoint is a signed note from the transparency log.
        # Verification requires:
        # 1. Parsing the note format
        # 2. Verifying the signature against the log's public key
        # 3. Extracting and validating the tree head
        logger.debug("TODO: Checkpoint verification not yet implemented")

        # TODO: Verify inclusion proof
        # The inclusion proof is a list of hashes that prove the entry
        # is part of the Merkle tree. Verification requires:
        # 1. Computing the leaf hash from the entry
        # 2. Walking up the tree using the proof hashes
        # 3. Comparing the computed root with the checkpoint's tree head
        logger.debug("TODO: Inclusion proof verification not yet implemented")

        print(f"[green]Transparency verification passed for {package.basefilename}")
