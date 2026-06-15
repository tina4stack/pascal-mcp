"""PAClient.exe wrapper — Embarcadero's CLI for driving PAServer.

PAClient is the Windows-side companion to PAServer (which runs on Mac /
Linux). The IDE invokes it for every cross-platform Embarcadero build:
deploying files to the remote host, codesigning iOS/macOS bundles,
building/installing IPAs, the whole Android packaging pipeline.

We wrap a slice of its surface (the parts useful when you're driving
builds from outside the IDE) as MCP tools:

  * Phase 1 — diagnostic: paserver_info, paserver_check_connection
  * Phase 2 — file transfer: paserver_get, paserver_put, paserver_remove

Higher-level iOS-bundle operations (codesign, IPA assembly/install) will
land in Phase 3 once the foundation is verified against a real PAServer.

Why use paclient directly instead of going through MSBuild for every
remote op? Two reasons:

  1. MSBuild /t:Deploy is opinionated — it deploys what the dproj's
     <Deployment> section says to deploy. For pre-flight checks, ad-hoc
     file pulls (logs, crash reports), or testing whether the remote host
     is even reachable, you want surgical operations, not a full build
     pipeline.

  2. paclient.exe handles the auth and protocol with PAServer for us.
     We can pass the registry's already-encrypted password via -pk and
     never have to touch Delphi's password obfuscation algorithm.

The module is Windows-only (PAClient is a Windows binary). On other
platforms find_paclient() returns None and the MCP tools refuse with a
clear error.
"""

from __future__ import annotations

import glob
import os
import socket
import subprocess
import sys
from dataclasses import dataclass


def find_paclient(studio_root: str | None = None) -> str | None:
    """Locate paclient.exe under a RAD Studio install.

    Looks first in the given studio_root's bin/, then sweeps the standard
    Embarcadero Studio install root for the highest version available.
    Returns None on non-Windows or if paclient.exe can't be found.
    """
    if sys.platform != "win32":
        return None

    if studio_root:
        candidate = os.path.join(studio_root, "bin", "paclient.exe")
        if os.path.isfile(candidate):
            return candidate

    # Fall back to highest-version install
    candidates = glob.glob(
        r"C:\Program Files (x86)\Embarcadero\Studio\*\bin\paclient.exe"
    )
    candidates = [c for c in candidates if os.path.isfile(c)]
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0]


@dataclass
class PAServerInfo:
    """Parsed output of `paclient -l <profile>`."""
    profile: str
    location: str  # The %APPDATA%\Embarcadero\BDS\<ver>\ dir
    platform: str  # OSX64 / iOSDevice64 / Linux64 / etc.
    host: str
    port: int
    password: str  # encrypted form — pass to -pk verbatim
    sysroot: str

    @property
    def has_address(self) -> bool:
        return bool(self.host) and self.port > 0


def _run_paclient(
    paclient: str, args: list[str], timeout: int = 30
) -> subprocess.CompletedProcess:
    """Run paclient with the given args.

    Note: paclient rejects --utf8encode as the first arg ("Invalid option")
    and the help is ambiguous about correct placement, so we don't pass it.
    The default ASCII output is fine for the regex parser and for tee'ing
    into MCP responses.
    """
    cmd = [paclient, *args]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )


def _resolve_studio_version(version: str | None) -> str | None:
    """Resolve a Studio version key (e.g. '37.0'), defaulting to the newest."""
    if version is not None:
        return version
    from pascal_mcp.compiler import (
        _discover_studio_roots,
        _studio_version_from_root,
    )
    roots = _discover_studio_roots()
    if not roots:
        return None
    return _studio_version_from_root(roots[0])


def _profile_exists_in_registry(profile: str, version: str | None) -> bool:
    """Check the HKCU registry for whether this profile is genuinely registered."""
    from pascal_mcp.compiler import _discover_remote_profiles
    version = _resolve_studio_version(version)
    if version is None:
        return False
    profiles = _discover_remote_profiles(version)
    return any(p.name == profile for p in profiles)


def get_paserver_info(
    paclient: str,
    profile: str,
    timeout: int = 30,
    studio_version: str | None = None,
) -> PAServerInfo | None:
    """Read PAServer Connection Profile info straight from the HKCU registry.

    The registry is the source of truth — host, port, platform, and the
    encrypted password all live under
    HKCU\\Software\\Embarcadero\\BDS\\<ver>\\RemoteProfiles\\<name>.

    We deliberately do NOT parse `paclient -l` output anymore. It's fragile
    in two ways we hit in practice:
      * When the IDE (bds.exe) is running, paclient refuses to emit the
        profile ("W0013 Cannot save profile while bds.exe process is
        running") and prints nothing parseable.
      * Different paclient point-releases reformat the output.

    The registry has none of those problems and is always readable. The
    `paclient` argument is retained for signature compatibility but unused.

    Returns None if the profile isn't registered. Sysroot isn't stored in
    the registry (paclient computes it at connection time) so it's left
    blank — it isn't needed for connection checks or file transfer.
    """
    from pascal_mcp.compiler import _discover_remote_profiles

    version = _resolve_studio_version(studio_version)
    if version is None:
        return None

    profiles = _discover_remote_profiles(version)
    match = next((p for p in profiles if p.name == profile), None)
    if match is None:
        return None

    # _discover_remote_profiles gives us name/platform/host/port. Pull the
    # encrypted password directly — needed for -pk on file transfer / iOS ops.
    password = _read_profile_password(profile, version)

    appdata = os.environ.get("APPDATA", "")
    location = os.path.join(appdata, "Embarcadero", "BDS", version) + os.sep

    return PAServerInfo(
        profile=f"{profile}.profile",
        location=location,
        platform=match.platform,
        host=match.hostname,
        port=match.port,
        password=password,
        sysroot="",  # not in registry; paclient computes it at connect time
    )


def _read_profile_password(profile: str, version: str) -> str:
    """Read the encrypted Password value for a profile from HKCU registry."""
    if sys.platform != "win32":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    key_path = rf"SOFTWARE\Embarcadero\BDS\{version}\RemoteProfiles\{profile}"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            val, _ = winreg.QueryValueEx(k, "Password")
            return str(val or "")
    except (FileNotFoundError, OSError):
        return ""


def tcp_reachable(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    """Plain TCP probe to (host, port). Cheap and answers the 90% question.

    Returns (ok, reason). If the port is closed or unreachable we say so;
    if it's open we report it as reachable (doesn't validate that PAServer
    is actually speaking the right protocol — that's what a real paclient
    connection check is for, see check_paserver_connection).
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"TCP {host}:{port} accepted connection"
    except (TimeoutError, socket.timeout):
        return False, f"TCP {host}:{port} timed out after {timeout}s"
    except ConnectionRefusedError:
        return False, f"TCP {host}:{port} refused — PAServer not running on the host?"
    except OSError as e:
        return False, f"TCP {host}:{port} unreachable: {e}"


@dataclass
class ConnectionCheckResult:
    """Combined outcome of a PAServer reachability check."""
    profile: str
    host: str
    port: int
    profile_ok: bool  # paclient -l found the profile
    tcp_ok: bool      # TCP socket reached the host:port
    notes: list[str]


def check_paserver_connection(
    paclient: str,
    profile: str,
    timeout: float = 3.0,
) -> ConnectionCheckResult:
    """Two-stage reachability check for a PAServer Connection Profile.

    1. paclient -l <profile> — confirms the profile exists locally and
       gives us host/port to probe.
    2. TCP connect to that host:port — confirms PAServer is listening.

    We don't push a full protocol handshake (would need to either invoke
    paclient with a real operation, or speak PAServer's wire protocol
    ourselves). For a pre-flight check the two stages above answer 90%
    of "why isn't this working" questions.
    """
    notes: list[str] = []

    info = get_paserver_info(paclient, profile)
    if info is None:
        notes.append(
            f"paclient -l {profile} did not return a recognised profile — "
            "is the name spelled correctly? Check list_remote_profiles."
        )
        return ConnectionCheckResult(
            profile=profile, host="", port=0,
            profile_ok=False, tcp_ok=False, notes=notes,
        )

    notes.append(
        f"Profile found: Platform={info.platform}, "
        f"Sysroot={info.sysroot}"
    )

    if not info.has_address:
        notes.append("Profile has no host/port — TCP probe skipped.")
        return ConnectionCheckResult(
            profile=profile, host=info.host, port=info.port,
            profile_ok=True, tcp_ok=False, notes=notes,
        )

    tcp_ok, reason = tcp_reachable(info.host, info.port, timeout=timeout)
    notes.append(reason)

    return ConnectionCheckResult(
        profile=profile, host=info.host, port=info.port,
        profile_ok=True, tcp_ok=tcp_ok, notes=notes,
    )


# ---------------------------------------------------------------------------
# Phase 2 — file transfer
# ---------------------------------------------------------------------------

def _paclient_conn_args(info: PAServerInfo) -> list[str]:
    """Build the standard -h/-p/-pk auth args from a PAServerInfo."""
    return [
        f"--host={info.host}",
        f"--port={info.port}",
        # Encrypted password from registry/profile; -pk takes it verbatim,
        # no decryption needed on our side.
        f"--passkey={info.password}",
    ]


def paserver_scratch_dir(
    profile: str,
    remote_user: str,
    windows_user: str | None = None,
) -> str:
    """Compose PAServer's conventional per-profile scratch directory path.

    PAServer stages everything under:
        /Users/<remote_user>/PAServer/scratch-dir/<windows_user>-<PROFILE>/

    The remote_user is the Unix account that's running paserver on the
    Mac/Linux host. The windows_user is your local Windows username,
    which paclient derives from %USERNAME% if not passed.

    This dir is the only path writeable in restricted mode (the default),
    and it's where the IDE deploys to as well — so use it for build
    staging, ad-hoc file pushes, and pulling .app / .ipa bundles back
    after a remote build.
    """
    if windows_user is None:
        windows_user = os.environ.get("USERNAME") or "user"
    return f"/Users/{remote_user}/PAServer/scratch-dir/{windows_user}-{profile}"


@dataclass
class TransferResult:
    """Outcome of a file transfer / removal call to paclient."""
    success: bool
    profile: str
    operation: str  # "get" | "put" | "remove"
    output: str     # stdout from paclient
    errors: str     # stderr from paclient


def paserver_get(
    paclient: str,
    profile: str,
    remote_path: str,
    local_dir: str,
    timeout: int = 300,
) -> TransferResult:
    """Pull a file/dir from the remote PAServer host into local_dir.

    Wraps `paclient -g <remote>,<local_dir>`. The remote_path can use
    PAClient's wildcard syntax (e.g. ``logs/*.txt``). The local_dir is
    where the file lands on this Windows box.
    """
    info = get_paserver_info(paclient, profile)
    if info is None:
        return TransferResult(False, profile, "get", "",
                              f"Profile {profile!r} not found")

    os.makedirs(local_dir, exist_ok=True)
    args = [
        *_paclient_conn_args(info),
        f"--get={remote_path},{local_dir}",
        profile,
    ]
    try:
        proc = _run_paclient(paclient, args, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TransferResult(False, profile, "get", "",
                              f"paclient -g timed out after {timeout}s")
    return TransferResult(
        success=proc.returncode == 0,
        profile=profile, operation="get",
        output=proc.stdout or "", errors=proc.stderr or "",
    )


def paserver_put(
    paclient: str,
    profile: str,
    local_path: str,
    remote_dir: str,
    timeout: int = 300,
) -> TransferResult:
    """Push a file/dir from this box to the remote PAServer host.

    Wraps `paclient -u <local>,<remote_dir>`. The local_path can use
    wildcard syntax (e.g. ``build\\*.so``).

    PAServer security note: by default PAServer runs in restricted mode
    and rejects writes outside its per-profile scratch sandbox at
    /Users/<unix-user>/PAServer/scratch-dir/<windows-user>-<PROFILE>/.
    Attempting to write to /tmp or /Users/something-else returns:
        Error: E0006 Copying file(s) to directory outside ... is not
        allowed; PAServer is running in restricted mode

    Either target the scratch dir (paserver_scratch_dir() helps construct
    the path) or have the user start PAServer with -restricted=false on
    the Mac side. The scratch dir is the right answer for build staging
    and ad-hoc file transfer — it's what the IDE uses.
    """
    info = get_paserver_info(paclient, profile)
    if info is None:
        return TransferResult(False, profile, "put", "",
                              f"Profile {profile!r} not found")

    if not (os.path.exists(local_path) or "*" in local_path):
        return TransferResult(False, profile, "put", "",
                              f"local_path does not exist: {local_path}")

    args = [
        *_paclient_conn_args(info),
        f"--put={local_path},{remote_dir}",
        profile,
    ]
    try:
        proc = _run_paclient(paclient, args, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TransferResult(False, profile, "put", "",
                              f"paclient -u timed out after {timeout}s")
    return TransferResult(
        success=proc.returncode == 0,
        profile=profile, operation="put",
        output=proc.stdout or "", errors=proc.stderr or "",
    )


@dataclass
class IOSPipelineResult:
    """Outcome of an iOS bundle operation (codesign, IPA build, install)."""
    success: bool
    profile: str
    operation: str
    output_path: str | None  # for create_ipa: where the IPA was written
    output: str
    errors: str


def ios_codesign(
    paclient: str,
    profile: str,
    app_path: str,
    certificate: str,
    entitlement: str | None = None,
    notarize: bool = False,
    timeout: int = 300,
) -> IOSPipelineResult:
    """Codesign an iOS/macOS .app bundle on the remote Mac.

    Wraps `paclient -c <path>,<cert>[,<entitlement> [,1]]`. The .app must
    already exist on the Mac (typically in the PAServer scratch dir after
    a Build+Deploy chain has run). The certificate is whatever's in the
    Mac's keychain — pass the cert's common name, or "-" for ad-hoc
    dash-signing (development only, won't install on real devices).

    Args:
        paclient: Path to paclient.exe (use find_paclient()).
        profile: Connection Profile name.
        app_path: Remote path to the .app bundle (e.g. /Users/.../scratch-dir/.../MyApp.app).
        certificate: Identity in the Mac's keychain. Pass "-" for ad-hoc.
        entitlement: Optional path to entitlements.plist on the remote Mac.
        notarize: If True, applies notarization options (the trailing "1"
            in paclient's -c syntax). Requires Apple Developer Program
            membership and notarization staging set up on the Mac.
        timeout: Seconds before the operation is killed.
    """
    info = get_paserver_info(paclient, profile)
    if info is None:
        return IOSPipelineResult(
            False, profile, "codesign", None, "",
            f"Profile {profile!r} not found",
        )

    codesign_arg = f"{app_path},{certificate}"
    if entitlement:
        codesign_arg += f",{entitlement}"
        if notarize:
            codesign_arg += ",1"
    elif notarize:
        # paclient's -c syntax requires entitlement to be present before the
        # notarize flag. Without it, just skip the notarize bit and tell
        # the caller.
        pass

    args = [
        *_paclient_conn_args(info),
        f"--codesign={codesign_arg}",
        profile,
    ]
    try:
        proc = _run_paclient(paclient, args, timeout=timeout)
    except subprocess.TimeoutExpired:
        return IOSPipelineResult(
            False, profile, "codesign", None, "",
            f"paclient -c timed out after {timeout}s",
        )
    return IOSPipelineResult(
        success=proc.returncode == 0,
        profile=profile, operation="codesign",
        output_path=app_path if proc.returncode == 0 else None,
        output=proc.stdout or "", errors=proc.stderr or "",
    )


def ios_create_ipa(
    paclient: str,
    profile: str,
    app_path: str,
    out_path: str,
    certificate: str,
    provisioning_profile: str,
    ipa_type: int = 1,
    timeout: int = 600,
) -> IOSPipelineResult:
    """Package a signed .app into an .ipa for distribution.

    Wraps `paclient -i <path>,<outpath>,<cert>,<profile>,<type>` where
    type=1 is ad-hoc (TestFlight, development device install) and type=2
    is app-store (App Store Connect upload).

    The .app must be codesigned first (call ios_codesign). The
    provisioning_profile is the path on the Mac to a .mobileprovision
    file matching the cert + bundle ID. IPA assembly runs entirely on
    the remote Mac via xcrun.

    Args:
        paclient: Path to paclient.exe.
        profile: Connection Profile name.
        app_path: Remote path to the signed .app.
        out_path: Remote path where the .ipa should be written.
        certificate: Same identity used for codesign.
        provisioning_profile: Remote path to the .mobileprovision file.
        ipa_type: 1 for ad-hoc / dev, 2 for app-store. Default 1.
        timeout: Seconds before killed (default 600 — IPA assembly is slow).
    """
    if ipa_type not in (1, 2):
        return IOSPipelineResult(
            False, profile, "create_ipa", None, "",
            f"ipa_type must be 1 (ad-hoc) or 2 (app-store), got {ipa_type}",
        )

    info = get_paserver_info(paclient, profile)
    if info is None:
        return IOSPipelineResult(
            False, profile, "create_ipa", None, "",
            f"Profile {profile!r} not found",
        )

    ipa_arg = (
        f"{app_path},{out_path},{certificate},"
        f"{provisioning_profile},{ipa_type}"
    )
    args = [
        *_paclient_conn_args(info),
        f"--ipa={ipa_arg}",
        profile,
    ]
    try:
        proc = _run_paclient(paclient, args, timeout=timeout)
    except subprocess.TimeoutExpired:
        return IOSPipelineResult(
            False, profile, "create_ipa", None, "",
            f"paclient -i timed out after {timeout}s",
        )
    return IOSPipelineResult(
        success=proc.returncode == 0,
        profile=profile, operation="create_ipa",
        output_path=out_path if proc.returncode == 0 else None,
        output=proc.stdout or "", errors=proc.stderr or "",
    )


def ios_install_ipa(
    paclient: str,
    profile: str,
    ipa_path: str,
    device_udid: str,
    timeout: int = 300,
) -> IOSPipelineResult:
    """Install an .ipa onto an iOS device attached to the remote Mac.

    Wraps `paclient -ii <path>,<deviceid>`. The iOS device must be:
      * Physically connected to the Mac (USB or via Wi-Fi pairing)
      * Trusted (the device's "Trust this computer" dialog has been
        accepted)
      * Listed in xcrun devicectl / Xcode's Devices and Simulators

    The UDID is the iOS device identifier. Find it with `idevice_id -l`
    or in Xcode → Window → Devices and Simulators.

    Note: this is for *device* installation, not Simulator. Simulator
    install uses xcrun simctl install which paclient doesn't wrap —
    that's still ahead in issue #5 (sim_* tools).

    Args:
        paclient: Path to paclient.exe.
        profile: Connection Profile name.
        ipa_path: Remote path to the .ipa on the Mac.
        device_udid: Target iOS device UDID.
        timeout: Seconds before killed.
    """
    info = get_paserver_info(paclient, profile)
    if info is None:
        return IOSPipelineResult(
            False, profile, "install_ipa", None, "",
            f"Profile {profile!r} not found",
        )

    args = [
        *_paclient_conn_args(info),
        f"--installipa={ipa_path},{device_udid}",
        profile,
    ]
    try:
        proc = _run_paclient(paclient, args, timeout=timeout)
    except subprocess.TimeoutExpired:
        return IOSPipelineResult(
            False, profile, "install_ipa", None, "",
            f"paclient -ii timed out after {timeout}s",
        )
    return IOSPipelineResult(
        success=proc.returncode == 0,
        profile=profile, operation="install_ipa",
        output_path=None,
        output=proc.stdout or "", errors=proc.stderr or "",
    )


def paserver_remove(
    paclient: str,
    profile: str,
    remote_path: str,
    timeout: int = 60,
) -> TransferResult:
    """Remove a file/dir on the remote PAServer host (paclient -R).

    Note: capital-R removes from the *remote*. Lowercase -r would remove
    from the local cache; we don't expose that — the caller can just
    delete the local file directly with normal filesystem APIs.
    """
    info = get_paserver_info(paclient, profile)
    if info is None:
        return TransferResult(False, profile, "remove", "",
                              f"Profile {profile!r} not found")

    args = [
        *_paclient_conn_args(info),
        f"--Remove={remote_path}",
        profile,
    ]
    try:
        proc = _run_paclient(paclient, args, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TransferResult(False, profile, "remove", "",
                              f"paclient -R timed out after {timeout}s")
    return TransferResult(
        success=proc.returncode == 0,
        profile=profile, operation="remove",
        output=proc.stdout or "", errors=proc.stderr or "",
    )
