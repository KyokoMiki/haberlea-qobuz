"""Qobuz API client for authentication and data retrieval."""

import time
from typing import Any

import msgspec
from aiohttp import ClientResponseError, ClientSession

from haberlea.utils.exceptions import (
    ModuleAPIError,
    ModuleAuthError,
    RegionRestrictedError,
)
from haberlea.utils.utils import create_aiohttp_session, hash_string

from .results import ApiSignature


class Qobuz:
    """Qobuz API client.

    Handles authentication and API requests to the Qobuz music service.

    Args:
        app_id: Qobuz application ID.
        app_secret: Qobuz application secret.
    """

    def __init__(self, app_id: str, app_secret: str) -> None:
        self.api_base = "https://www.qobuz.com/api.json/0.2/"
        self.app_id = app_id
        self.app_secret = app_secret
        self.auth_token: str | None = None
        self.session: ClientSession = create_aiohttp_session()

    async def close(self) -> None:
        """Close the aiohttp session."""
        if not self.session.closed:
            await self.session.close()

    @property
    def _headers(self) -> dict[str, str]:
        """Request headers for API calls.

        Returns:
            Dictionary of HTTP headers for API requests.
        """
        headers = {
            "X-Device-Platform": "android",
            "X-Device-Model": "Pixel 3",
            "X-Device-Os-Version": "10",
            "X-Device-Manufacturer-Id": "482D8CB7-015D-402F-A93B-5EEF0E0996F3",
            "X-App-Version": "5.16.1.5",
            "X-App-Id": self.app_id,
            "User-Agent": (
                "Dalvik/2.1.0 (Linux; U; Android 10; Pixel 3 Build/QP1A.190711.020))"
                "QobuzMobileAndroid/5.16.1.5-b21041415"
            ),
        }
        if self.auth_token:
            headers["X-User-Auth-Token"] = self.auth_token
        return headers

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> dict:
        """Make a GET request to the API.

        Args:
            url: API endpoint path.
            params: Query parameters.

        Returns:
            JSON response as dictionary.

        Raises:
            RegionRestrictedError: If content is region-restricted (404).
            ModuleAPIError: If the request fails for other reasons.
        """
        if params is None:
            params = {}

        try:
            async with self.session.get(
                f"{self.api_base}{url}", params=params, headers=self._headers
            ) as response:
                response.raise_for_status()
                return msgspec.json.decode(await response.read())

        except ClientResponseError as e:
            # 404 on content endpoints indicates region restriction
            if e.status == 404 and url in (
                "album/get",
                "track/get",
                "track/getFileUrl",
                "playlist/get",
                "artist/get",
            ):
                # Extract content ID from params
                content_id = (
                    params.get("album_id")
                    or params.get("track_id")
                    or params.get("playlist_id")
                    or params.get("artist_id")
                    or "unknown"
                )
                content_type = url.split("/")[0]
                raise RegionRestrictedError(
                    content_id=str(content_id),
                    content_type=content_type,
                    module_name="qobuz",
                ) from e

            raise ModuleAPIError(
                error_code=e.status,
                error_message=e.message,
                api_endpoint=url,
                module_name="qobuz",
            ) from e

    async def login(self, email: str, password: str) -> str:
        """Authenticate with Qobuz.

        Args:
            email: User email or user ID.
            password: User password or auth token.

        Returns:
            User authentication token.

        Raises:
            Exception: If login fails or account is not eligible.
        """
        if "@" in email:
            params = {
                "username": email,
                "password": hash_string(password, "MD5"),
                "app_id": self.app_id,
            }
        else:
            params = {
                "user_id": email,
                "user_auth_token": password,
                "app_id": self.app_id,
            }

        signature = self._create_signature("user/login", params)
        params["request_ts"] = signature.timestamp
        params["request_sig"] = signature.signature

        r = await self._get("user/login", params)

        # Safely validate response structure
        if r.get("user_auth_token") and r.get("user", {}).get("credential", {}).get(
            "parameters"
        ):
            self.auth_token = r["user_auth_token"]
            return r["user_auth_token"]

        raise ModuleAuthError(module_name="qobuz")

    def _create_signature(self, method: str, parameters: dict) -> ApiSignature:
        """Create API request signature.

        Args:
            method: API method name.
            parameters: Request parameters.

        Returns:
            ApiSignature with timestamp and signature.
        """
        timestamp = str(int(time.time()))
        to_hash = method.replace("/", "")

        for key in sorted(parameters.keys()):
            if key not in ("app_id", "user_auth_token"):
                to_hash += key + str(parameters[key])

        to_hash += timestamp + self.app_secret
        signature = hash_string(to_hash, "MD5")
        return ApiSignature(signature=signature, timestamp=timestamp)

    async def search(self, query_type: str, query: str, limit: int = 10) -> dict:
        """Search for content.

        Args:
            query_type: Type of content (track, album, artist, playlist).
            query: Search query string.
            limit: Maximum number of results.

        Returns:
            Search results dictionary.
        """
        return await self._get(
            "catalog/search",
            {
                "query": query,
                "type": query_type + "s",
                "limit": limit,
                "app_id": self.app_id,
            },
        )

    async def get_file_url(self, track_id: str, quality_id: int = 27) -> dict:
        """Get track download URL.

        Args:
            track_id: Track identifier.
            quality_id: Quality format ID (5=MP3, 6=16bit FLAC, 27=24bit FLAC).

        Returns:
            File URL and format information.
        """
        params = {
            "track_id": track_id,
            "format_id": str(quality_id),
            "intent": "stream",
            "sample": "false",
            "app_id": self.app_id,
            "user_auth_token": self.auth_token,
        }

        signature = self._create_signature("track/getFileUrl", params)
        params["request_ts"] = signature.timestamp
        params["request_sig"] = signature.signature

        return await self._get("track/getFileUrl", params)

    async def get_track(self, track_id: str) -> dict:
        """Get track metadata.

        Args:
            track_id: Track identifier.

        Returns:
            Track metadata dictionary.
        """
        return await self._get(
            "track/get", params={"track_id": track_id, "app_id": self.app_id}
        )

    async def get_playlist(self, playlist_id: str) -> dict:
        """Get playlist metadata and tracks.

        Args:
            playlist_id: Playlist identifier.

        Returns:
            Playlist metadata dictionary.
        """
        return await self._get(
            "playlist/get",
            params={
                "playlist_id": playlist_id,
                "app_id": self.app_id,
                "limit": "2000",
                "offset": "0",
                "extra": "tracks,subscribers,focusAll",
            },
        )

    async def get_album(self, album_id: str) -> dict:
        """Get album metadata and tracks.

        Args:
            album_id: Album identifier.

        Returns:
            Album metadata dictionary.
        """
        return await self._get(
            "album/get",
            params={
                "album_id": album_id,
                "app_id": self.app_id,
                "extra": "albumsFromSameArtist,focusAll",
            },
        )

    async def get_artist(self, artist_id: str) -> dict:
        """Get artist metadata and discography.

        Args:
            artist_id: Artist identifier.

        Returns:
            Artist metadata dictionary.
        """
        extra_fields = (
            "albums,playlists,tracks_appears_on,albums_with_last_release,focusAll"
        )
        return await self._get(
            "artist/get",
            params={
                "artist_id": artist_id,
                "app_id": self.app_id,
                "extra": extra_fields,
                "limit": "1000",
                "offset": "0",
            },
        )
