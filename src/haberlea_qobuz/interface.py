"""Qobuz module interface for Haberlea."""

import asyncio
import unicodedata
from datetime import UTC, datetime
from hashlib import md5
from typing import Any

import av
from av import AudioFrame
from av.audio.resampler import AudioResampler
from av.packet import Packet
from mutagen.flac import FLAC
from numpy import uint8
from rich import print

from haberlea.plugins.base import ModuleBase
from haberlea.utils.models import (
    AlbumInfo,
    ArtistInfo,
    CodecEnum,
    CodecOptions,
    CreditsInfo,
    DownloadEnum,
    DownloadTypeEnum,
    ModuleController,
    ModuleInformation,
    ModuleModes,
    PlaylistInfo,
    QualityEnum,
    SearchResult,
    Tags,
    TrackDownloadInfo,
    TrackInfo,
)
from haberlea.utils.utils import download_file

from .qobuz_api import Qobuz

module_information = ModuleInformation(
    service_name="Qobuz",
    module_supported_modes=ModuleModes.download | ModuleModes.credits,
    global_settings={
        "app_id": "",
        "app_secret": "",
        "quality_format": "{sample_rate}kHz {bit_depth}bit",
    },
    session_settings={"username": "", "password": ""},
    session_storage_variables=["token"],
    netlocation_constant="qobuz",
    url_constants={
        "track": DownloadTypeEnum.track,
        "album": DownloadTypeEnum.album,
        "playlist": DownloadTypeEnum.playlist,
        "artist": DownloadTypeEnum.artist,
        "interpreter": DownloadTypeEnum.artist,
    },
    test_url="https://open.qobuz.com/track/52151405",
)


class ModuleInterface(ModuleBase):
    """Qobuz module interface implementation.

    Handles authentication, metadata retrieval, and track downloading
    from the Qobuz music streaming service.
    """

    def __init__(self, module_controller: ModuleController) -> None:
        """Initialize the Qobuz module.

        Args:
            module_controller: Controller providing access to settings and resources.
        """
        super().__init__(module_controller)
        settings = module_controller.module_settings
        self.api = Qobuz(settings["app_id"], settings["app_secret"])
        self.api.auth_token = module_controller.temporary_settings_controller.read(
            "token"
        )

        # 5 = 320 kbps MP3, 6 = 16-bit FLAC,
        # 7 = 24-bit / =< 96kHz FLAC, 27 =< 192 kHz FLAC
        self.quality_parse: dict[QualityEnum, int] = {
            QualityEnum.MINIMUM: 5,
            QualityEnum.LOW: 5,
            QualityEnum.MEDIUM: 5,
            QualityEnum.HIGH: 5,
            QualityEnum.LOSSLESS: 6,
            QualityEnum.HIFI: 27,
        }
        self.quality_tier = module_controller.haberlea_options.quality_tier
        self.quality_format: str | None = settings.get("quality_format")

    async def close(self) -> None:
        """Close the module and release resources.

        Closes the underlying Qobuz API client session to prevent
        unclosed aiohttp ClientSession warnings.
        """
        await self.api.close()

    async def login(self, email: str, password: str) -> None:
        """Authenticate with Qobuz.

        Args:
            email: User email.
            password: User password.
        """
        token = await self.api.login(email, password)
        self.api.auth_token = token
        self.module_controller.temporary_settings_controller.set("token", token)

    def _extract_track_artists(
        self, track_data: dict, album_data: dict
    ) -> tuple[list[str], dict]:
        """Extract and process artist information from track data.

        Args:
            track_data: Track data dictionary.
            album_data: Album data dictionary.

        Returns:
            Tuple of (artists list, modified track_data).
        """
        main_artist = track_data.get("performer", album_data["artist"])
        artists = [
            unicodedata.normalize("NFKD", main_artist["name"])
            .encode("ascii", "ignore")
            .decode("utf-8")
        ]

        # Filter MainArtist and FeaturedArtist from performers
        if track_data.get("performers"):
            performers = []
            for credit in track_data["performers"].split(" - "):
                contributor_role = credit.split(", ")[1:]
                contributor_name = credit.split(", ")[0]

                for contributor in ["MainArtist", "FeaturedArtist", "Artist"]:
                    if contributor in contributor_role:
                        if contributor_name not in artists:
                            artists.append(contributor_name)
                        contributor_role.remove(contributor)

                if not contributor_role:
                    continue
                performers.append(f"{contributor_name}, {', '.join(contributor_role)}")
            track_data["performers"] = " - ".join(performers)

        artists[0] = main_artist["name"]
        return artists, track_data

    def _build_qobuz_track_tags(self, track_data: dict, album_data: dict) -> Tags:
        """Build Tags object from track and album data.

        Args:
            track_data: Track data dictionary.
            album_data: Album data dictionary.

        Returns:
            Tags object with metadata.
        """
        label_data = album_data.get("label")
        label_name = label_data.get("name") if isinstance(label_data, dict) else None

        audio_info = track_data.get("audio_info")
        replay_gain = None
        replay_peak = None
        if isinstance(audio_info, dict):
            replay_gain = audio_info.get("replaygain_track_gain")
            replay_peak = audio_info.get("replaygain_track_peak")

        return Tags(
            album_artist=album_data["artist"]["name"],
            composer=track_data["composer"]["name"]
            if "composer" in track_data
            else None,
            release_date=album_data.get("release_date_original"),
            track_number=track_data["track_number"],
            total_tracks=album_data["tracks_count"],
            disc_number=track_data["media_number"],
            total_discs=album_data["media_count"],
            isrc=track_data.get("isrc"),
            upc=album_data.get("upc"),
            label=label_name,
            copyright=album_data.get("copyright"),
            genres=[album_data["genre"]["name"]],
            replay_gain=replay_gain,
            replay_peak=replay_peak,
        )

    def _calculate_bitrate(self, stream_data: dict) -> int | None:
        """Calculate bitrate from stream data.

        Args:
            stream_data: Stream data dictionary.

        Returns:
            Bitrate in kbps, or None if unavailable.
        """
        # uncompressed PCM bitrate calculation
        bitrate = 320
        if stream_data.get("format_id") in {6, 7, 27}:
            bitrate = int(
                (stream_data["sampling_rate"] * 1000 * stream_data["bit_depth"] * 2)
                // 1000
            )
        elif not stream_data.get("format_id"):
            bitrate = stream_data.get("format_id")
        return bitrate

    def _build_track_name(self, track_data: dict) -> str:
        """Build track name with work and version tags.

        Args:
            track_data: Track data dictionary.

        Returns:
            Formatted track name.
        """
        track_title = track_data.get("title") or ""
        track_name = f"{track_data.get('work')} - " if track_data.get("work") else ""
        track_name += track_title.rstrip()
        track_name += (
            f" ({track_data.get('version')})" if track_data.get("version") else ""
        )
        return track_name

    def _build_album_name(self, album_data: dict) -> str:
        """Build album name with version tag.

        Args:
            album_data: Album data dictionary.

        Returns:
            Formatted album name.
        """
        album_title = album_data.get("title") or ""
        album_name = album_title.rstrip()
        album_name += (
            f" ({album_data.get('version')})" if album_data.get("version") else ""
        )
        return album_name

    async def get_track_info(
        self,
        track_id: str,
        quality_tier: QualityEnum,
        codec_options: CodecOptions,  # noqa: ARG002
        data: dict | None = None,
    ) -> TrackInfo:
        """Get track information and metadata.

        Args:
            track_id: Track identifier.
            quality_tier: Desired audio quality.
            codec_options: Codec preference options (unused).
            data: Optional pre-fetched track data.

        Returns:
            TrackInfo with metadata and download information.
        """
        if data is None:
            data = {}
        track_data = (
            data[track_id] if track_id in data else await self.api.get_track(track_id)
        )
        album_data = track_data["album"]

        quality_id: int = self.quality_parse[quality_tier]

        # Extract artists
        artists, track_data = self._extract_track_artists(track_data, album_data)

        # Build tags
        tags = self._build_qobuz_track_tags(track_data, album_data)

        # Get stream data and calculate bitrate
        stream_data = await self.api.get_file_url(track_id, quality_id)
        bitrate = self._calculate_bitrate(stream_data)

        # Build track and album names
        track_name = self._build_track_name(track_data)
        album_name = self._build_album_name(album_data)

        # Determine codec
        main_artist = track_data.get("performer", album_data["artist"])

        return TrackInfo(
            name=track_name,
            album_id=album_data["id"],
            album=album_name,
            artists=artists,
            artist_id=main_artist["id"],
            bit_depth=stream_data["bit_depth"],
            bitrate=bitrate,
            sample_rate=stream_data["sampling_rate"],
            release_year=int(album_data["release_date_original"].split("-")[0]),
            explicit=track_data["parental_warning"],
            cover_url=album_data["image"]["large"].split("_")[0] + "_org.jpg",
            tags=tags,
            codec=CodecEnum.FLAC
            if stream_data.get("format_id") in {6, 7, 27}
            else CodecEnum.NONE
            if not stream_data.get("format_id")
            else CodecEnum.MP3,
            duration=track_data.get("duration"),
            credits_data={track_id: track_data},
            download_url=stream_data.get("url"),
            error=f'Track "{track_data["title"]}" is not streamable!'
            if not track_data["streamable"]
            else None,
        )

    async def get_track_download(
        self,
        target_path: str,
        url: str = "",
        data: dict | None = None,  # noqa: ARG002
    ) -> TrackDownloadInfo:
        """Download track file directly to target path.

        Args:
            target_path: Target file path for direct download.
            url: The URL to download the track from.
            data: Optional extra data for download (unused in Qobuz).

        Returns:
            TrackDownloadInfo indicating download type.
        """
        # Download directly to target path using module's session
        await download_file(url, target_path, session=self.api.session)

        # Add MD5 signature for FLAC files (runs in thread pool)
        await asyncio.to_thread(self.add_flac_md5_signature, target_path)

        return TrackDownloadInfo(download_type=DownloadEnum.DIRECT)

    def add_flac_md5_signature(self, file_path: str) -> None:
        """Add MD5 signature to FLAC file if missing.

        This is called as a post-download hook by the downloader.

        Args:
            file_path: Path to the downloaded FLAC file.
        """
        try:
            flac_file = FLAC(file_path)
            if flac_file is None or flac_file.info.md5_signature != 0:
                return

            bit_depth = flac_file.info.bits_per_sample
            md5_hash = self._calculate_flac_md5(file_path, bit_depth)
            if not md5_hash:
                return

            flac_file.info.md5_signature = int.from_bytes(md5_hash, "big")
            flac_file.save()

        except Exception as e:
            # If it's not a FLAC file or MD5 calculation fails, continue without MD5
            print(f"Failed to add FLAC MD5 signature: {e}")

    def _calculate_flac_md5(self, flac_path: str, bit_depth: int) -> bytes:
        """Calculate MD5 hash for FLAC file.

        Decodes the FLAC file to raw PCM samples and calculates the MD5 hash.
        Uses PyAV for decoding instead of ffmpeg-python.

        The FLAC MD5 signature is calculated from interleaved little-endian PCM
        data (s16le for 16-bit, s24le for 24-bit). Since PyAV doesn't support
        s24 natively, we use s32 for 24-bit and strip the padding byte.

        Args:
            flac_path: Path to the FLAC file.
            bit_depth: Bit depth of the audio (16 or 24).

        Returns:
            MD5 hash as bytes, or empty bytes if decoding fails.
        """
        md_5 = md5()
        try:
            with av.open(flac_path) as container:
                audio_stream = None
                for stream in container.streams:
                    if stream.type == "audio":
                        audio_stream = stream
                        break

                if audio_stream is None:
                    return b""

                # Get codec context for stream info
                codec_ctx = audio_stream.codec_context
                if codec_ctx is None:
                    return b""

                stream_layout = getattr(codec_ctx, "layout", None)
                stream_rate = getattr(codec_ctx, "rate", None)
                if stream_layout is None or stream_rate is None:
                    return b""

                # Use s32 for 24-bit (PyAV doesn't support s24 natively)
                output_format = "s32" if bit_depth == 24 else "s16"

                resampler = av.AudioResampler(
                    format=output_format,
                    layout=stream_layout,
                    rate=stream_rate,
                )

                for packet in container.demux(audio_stream):
                    self._process_md5_packet(packet, resampler, bit_depth, md_5)

            return md_5.digest()

        except Exception:
            return b""

    def _process_md5_packet(
        self,
        packet: Packet,
        resampler: AudioResampler,
        bit_depth: int,
        md_5: Any,
    ) -> None:
        """Process a single audio packet for MD5 calculation.

        Args:
            packet: The audio packet to process.
            resampler: The audio resampler.
            bit_depth: The target bit depth.
            md_5: The MD5 hash object to update.
        """
        for frame in packet.decode():
            if not isinstance(frame, AudioFrame):
                continue

            # Resample to packed format
            resampled_frames = resampler.resample(frame)
            if resampled_frames is None:
                continue

            frames_list = (
                resampled_frames
                if isinstance(resampled_frames, list)
                else [resampled_frames]
            )

            for resampled in frames_list:
                if not isinstance(resampled, AudioFrame):
                    continue

                # Get interleaved PCM data as numpy array
                arr = resampled.to_ndarray()
                if arr.ndim == 2:
                    arr = arr.T

                if bit_depth == 24:
                    # Convert s32le to s24le: take bytes [1:4] from each sample
                    arr = arr.view(uint8).reshape(-1, 4)[:, 1:4].ravel()

                # md5.update() accepts buffer protocol objects directly
                md_5.update(arr)

    async def get_album_info(
        self, album_id: str, data: dict | None = None
    ) -> AlbumInfo:
        """Get album information and track list.

        Args:
            album_id: Album identifier.
            data: Optional pre-fetched album data (unused in Qobuz).

        Returns:
            AlbumInfo with metadata and track list.
        """
        album_data = await self.api.get_album(album_id)
        booklet_url = (
            album_data["goodies"][0]["url"]
            if "goodies" in album_data and len(album_data["goodies"]) != 0
            else None
        )

        tracks, extra_kwargs = [], {}
        for track in album_data.pop("tracks")["items"]:
            track_id = str(track["id"])
            tracks.append(track_id)
            track["album"] = album_data
            extra_kwargs[track_id] = track

        # get the wanted quality for an actual album quality_format string
        quality_tier = self.quality_parse[self.quality_tier]
        # TODO: Ignore sample_rate and bit_depth if album_data['hires'] is False?
        bit_depth = 24 if quality_tier == 27 and album_data["hires_streamable"] else 16
        sample_rate = (
            album_data["maximum_sampling_rate"]
            if quality_tier == 27 and album_data["hires_streamable"]
            else 44.1
        )

        quality_tags = {"sample_rate": sample_rate, "bit_depth": bit_depth}

        # album title fix to include version tag
        album_title_raw = album_data.get("title") or ""
        album_name = album_title_raw.rstrip()
        album_name += (
            f" ({album_data.get('version')})" if album_data.get("version") else ""
        )

        quality_str: str | None = None
        if self.quality_format:
            quality_str = self.quality_format.format(**quality_tags)

        return AlbumInfo(
            name=album_name,
            artist=album_data["artist"]["name"],
            artist_id=album_data["artist"]["id"],
            tracks=tracks,
            release_year=int(album_data["release_date_original"].split("-")[0]),
            explicit=album_data["parental_warning"],
            quality=quality_str,
            description=album_data.get("description"),
            cover_url=album_data["image"]["large"].split("_")[0] + "_org.jpg",
            all_track_cover_jpg_url=album_data["image"]["large"],
            upc=album_data.get("upc"),
            duration=album_data.get("duration"),
            booklet_url=booklet_url,
            track_data=extra_kwargs,
        )

    async def get_playlist_info(self, playlist_id: str) -> PlaylistInfo:
        """Get playlist information and track list.

        Args:
            playlist_id: Playlist identifier.

        Returns:
            PlaylistInfo with metadata and track list.
        """
        playlist_data = await self.api.get_playlist(playlist_id)

        tracks, extra_kwargs = [], {}
        for track in playlist_data["tracks"]["items"]:
            track_id = str(track["id"])
            extra_kwargs[track_id] = track
            tracks.append(track_id)

        return PlaylistInfo(
            name=playlist_data["name"],
            creator=playlist_data["owner"]["name"],
            creator_id=playlist_data["owner"]["id"],
            release_year=int(
                datetime.fromtimestamp(playlist_data["created_at"], tz=UTC).strftime(
                    "%Y"
                )
            ),
            description=playlist_data.get("description"),
            duration=playlist_data.get("duration"),
            tracks=tracks,
            track_data=extra_kwargs,
        )

    async def get_artist_info(
        self,
        artist_id: str,
        get_credited_albums: bool = False,  # noqa: ARG002
    ) -> ArtistInfo:
        """Get artist information and discography.

        Args:
            artist_id: Artist identifier.
            get_credited_albums: Whether to include credited albums.

        Returns:
            ArtistInfo with metadata and album list.
        """
        artist_data = await self.api.get_artist(artist_id)
        albums = [str(album["id"]) for album in artist_data["albums"]["items"]]

        return ArtistInfo(name=artist_data["name"], albums=albums)

    async def get_track_credits(
        self, track_id: str, data: dict | None = None
    ) -> list[CreditsInfo]:
        """Get track credits information.

        Args:
            track_id: Track identifier.
            data: Optional pre-fetched track data.

        Returns:
            List of CreditsInfo with contributor information.
        """
        track_data = (
            data[track_id]
            if data and track_id in data
            else await self.api.get_track(track_id)
        )
        track_contributors = track_data.get("performers")

        # Credits look like: {name}, {type1}, {type2} - {name2}, {type2}
        credits_dict: dict[str, list[str]] = {}
        if track_contributors:
            for credit in track_contributors.split(" - "):
                contributor_role = credit.split(", ")[1:]
                contributor_name = credit.split(", ")[0].strip()

                for role in contributor_role:
                    # Strip whitespace and control characters from role
                    role = role.strip()
                    if not role:
                        continue
                    # Check if the dict contains no list, create one
                    if role not in credits_dict:
                        credits_dict[role] = []
                    # Now add the name to the type list
                    credits_dict[role].append(contributor_name)

        # Convert the dictionary back to a list of CreditsInfo
        return [CreditsInfo(k, v) for k, v in credits_dict.items()]

    async def search(
        self,
        query_type: DownloadTypeEnum,
        query: str,
        track_info: TrackInfo | None = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        """Search for content on Qobuz.

        Args:
            query_type: Type of content to search for.
            query: Search query string.
            track_info: Optional track info for ISRC-based search.
            limit: Maximum number of results.

        Returns:
            List of SearchResult objects.
        """
        results = {}
        if track_info and track_info.tags.isrc:
            results = await self.api.search(
                query_type.name, track_info.tags.isrc, limit
            )
        if not results:
            results = await self.api.search(query_type.name, query, limit)

        items = []
        for i in results[query_type.name + "s"]["items"]:
            duration = None
            year: str | None = None
            artists: list[str] | None = None
            if query_type is DownloadTypeEnum.artist:
                pass
            elif query_type is DownloadTypeEnum.playlist:
                artists = [i["owner"]["name"]]
                year = datetime.fromtimestamp(i["created_at"], tz=UTC).strftime("%Y")
                duration = i["duration"]
            elif query_type is DownloadTypeEnum.track:
                artists = [i["performer"]["name"]]
                year = i["album"]["release_date_original"].split("-")[0]
                duration = i["duration"]
            elif query_type is DownloadTypeEnum.album:
                artists = [i["artist"]["name"]]
                year = i["release_date_original"].split("-")[0]
                duration = i["duration"]
            else:
                raise ValueError("Query type is invalid")
            name = i.get("name") or i.get("title")
            name += f" ({i.get('version')})" if i.get("version") else ""
            item = SearchResult(
                name=name,
                artists=artists,
                year=year,
                result_id=str(i["id"]),
                explicit=bool(i.get("parental_warning")),
                additional=[
                    f"{i['maximum_sampling_rate']}kHz/{i['maximum_bit_depth']}bit"
                ]
                if "maximum_sampling_rate" in i
                else None,
                duration=duration,
                data={str(i["id"]): i}
                if query_type is DownloadTypeEnum.track
                else None,
            )

            items.append(item)

        return items
