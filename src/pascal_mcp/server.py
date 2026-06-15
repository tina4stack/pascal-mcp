"""Claude Pascal MCP Server.

Exposes Pascal/Delphi compilation and execution tools via the
Model Context Protocol (MCP) for use with Claude.
"""

import base64

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.utilities.types import Image

from pascal_mcp.compiler import (
    _discover_remote_profiles,
    _discover_studio_roots,
    _studio_version_from_root,
    build_existing_dproj,
    cleanup_compile_result,
    compile_and_launch,
    compile_project,
    compile_source,
    detect_compilers,
    run_source,
)
from pascal_mcp.templates import (
    generate_console_project,
    generate_fmx_project,
    generate_fpc_project,
    generate_vcl_project,
)
from pascal_mcp.form_parser import (
    format_component_list,
    format_summary,
    format_tree,
    parse_form_file,
)
from pascal_mcp.installer import download_and_install_fpc
from pascal_mcp.screenshot import capture_window, list_windows
from pascal_mcp.adb import (
    capture_device_screen,
    get_device_info,
    install_apk,
    key_event,
    launch_app as _adb_launch_app_impl,
    list_devices,
    list_packages,
    pull_file,
    push_file,
    stop_app,
    swipe,
    tap,
    type_text,
)
from pascal_mcp.win_interact import (
    click_window,
    type_in_window,
    send_key_to_window,
)
from pascal_mcp.ide_observer import (
    capture_ide_screenshot,
    find_ide_window,
    find_project_files,
    read_source_context,
    resolve_error_file,
)

mcp = FastMCP(
    "pascal-dev",
    instructions=(
        "IMPORTANT: Always use these tools for Pascal/Delphi compilation "
        "and execution. NEVER use MSBuild, shell commands, or other build "
        "systems directly — these tools handle all compiler invocation, "
        "project structure, and output capture automatically. "
        "Use get_compiler_info to check available compilers. "
        "Use compile_pascal to compile single-file source code. "
        "Use build_dproj to build an EXISTING .dproj file (multi-unit, "
        "real Delphi project with its own search paths, defines, resources). "
        "Use compile_delphi_project ONLY to GENERATE a new throwaway project "
        "from a template (TButton/TEdit/TLabel/TMemo) — it cannot build "
        "an existing .dproj. "
        "Use run_pascal to compile and execute console programs. "
        "Use launch_app for GUI applications that need to stay running. "
        "If no compiler is found, use setup_fpc to install Free Pascal. "
        "Use parse_form to read DFM/FMX/LFM form files. "
        "Use screenshot_app to capture Windows app windows, then "
        "app_click, app_type, and app_key to interact with them. "
        "Use adb_devices to list connected Android devices. Use adb_screenshot "
        "to capture the device screen. Use adb_tap, adb_swipe, adb_type_text, "
        "and adb_key for UI automation. Use adb_install, adb_launch_app, "
        "adb_stop_app for app management. Use adb_push and adb_pull for "
        "file transfer. All ADB tools accept an optional device serial. "
        "PASERVER & PACLIENT: for any work that talks to a remote Mac or "
        "Linux host via PAServer (iOS/macOS/Linux builds, file transfer "
        "to/from the remote host, iOS codesign / IPA / device install), "
        "use the paserver_* and ios_* tools — NEVER shell out to paclient.exe "
        "directly or roll your own SSH layer. paserver_check_connection "
        "is the right pre-flight before any iOS/macOS/Linux operation. "
        "list_remote_profiles enumerates the Connection Profiles registered "
        "in HKCU. paserver_get / paserver_put / paserver_remove transfer "
        "files (PAServer's restricted mode means writes go in the per-profile "
        "scratch dir — paserver_scratch_dir composes it). After a build_dproj "
        "for iOS leaves a .app in scratch, ios_codesign / ios_create_ipa / "
        "ios_install_ipa wrap the codesign + IPA + device-install pipeline. "
        "iOS SIMULATOR: PAClient does not control xcrun simctl, so simulator "
        "operations go through SSH-to-Mac instead. Use sim_list / sim_boot / "
        "sim_install / sim_launch / sim_terminate / sim_uninstall / "
        "sim_open_url / sim_screenshot — same surface as the adb_* tools. "
        "Generic remote command execution is mac_ssh_run; mac_ssh_check is "
        "the pre-flight. SSH key auth must be configured once with "
        "`ssh-copy-id <user>@<host>` — these tools never accept passwords."
    ),
)


@mcp.tool()
async def get_compiler_info(ctx: Context) -> str:
    """Detect available Pascal compilers and return their details.

    Checks for Free Pascal (fpc), Delphi 32-bit (dcc32), and Delphi 64-bit (dcc64)
    on the system PATH and in common installation directories.

    Returns a summary of all compilers found with name, version, and path.

    Side effect: also emits an MCP `notifications/tools/list_changed` so any
    new tools added to the server since the client last fetched its catalog
    become visible without a Claude Code restart. Cheap to call; if the client
    doesn't honour the notification, nothing breaks.
    """
    compilers = detect_compilers()

    # Best-effort: poke the client to re-fetch tools/list. This lets new tools
    # added in subsequent versions of this MCP show up after a server respawn
    # without needing a full Claude Code restart — the client either honours
    # the standard MCP notification or silently ignores it. Either way the
    # tool's primary purpose (compiler detection) still works.
    try:
        await ctx.session.send_tool_list_changed()
    except Exception:
        pass

    if not compilers:
        return (
            "No Pascal compilers found on this system.\n\n"
            "Available options:\n"
            "  - Use the setup_fpc tool to download and install Free Pascal\n"
            "  - Install Lazarus IDE (includes FPC): https://www.lazarus-ide.org\n"
            "  - Install RAD Studio: https://www.embarcadero.com/products/rad-studio"
        )

    lines = [f"Found {len(compilers)} Pascal compiler(s):\n"]
    for c in compilers:
        lines.append(f"  [{c.compiler_type}] {c.name}")
        lines.append(f"    Version: {c.version}")
        lines.append(f"    Path:    {c.path}")
        lines.append("")

    # PAClient detection — useful both as a signal to Claude that PAServer
    # tooling is available, and as a diagnostic hint when the user reports
    # PAServer-related issues. Best-effort; never block compiler info.
    try:
        from pascal_mcp.paclient import find_paclient
        pc = find_paclient()
        if pc:
            lines.append("PAClient (PAServer driver):")
            lines.append(f"    Path:    {pc}")
            lines.append(
                "    Tools:   paserver_check_connection, paserver_info, "
                "paserver_get/put/remove, ios_codesign, ios_create_ipa, "
                "ios_install_ipa, list_remote_profiles"
            )
            lines.append("")
    except Exception:
        pass

    return "\n".join(lines)


@mcp.tool()
async def compile_pascal(
    source_code: str,
    compiler: str | None = None,
) -> str:
    """Compile Pascal source code and return compiler output.

    Use this to check if code compiles without running it. Returns compiler
    messages including any errors or warnings.

    Args:
        source_code: The complete Pascal source code to compile. Should include
            the program/unit header (e.g., 'program Hello;').
        compiler: Which compiler to use. Can be a type name ('fpc', 'dcc32',
            'dcc64') or a full path to a specific compiler executable (e.g.,
            'C:\\Program Files (x86)\\Embarcadero\\Studio\\37.0\\bin\\dcc64.exe').
            If not specified, auto-selects the best available compiler.
    """
    result = compile_source(source_code, compiler_type=compiler)

    parts = [f"Compiler: {result.compiler_used}"]
    parts.append(f"Success: {result.success}")
    parts.append(f"Exit code: {result.exit_code}")

    if result.stdout.strip():
        parts.append(f"\n--- Compiler Output ---\n{result.stdout.strip()}")
    if result.stderr.strip():
        parts.append(f"\n--- Compiler Messages ---\n{result.stderr.strip()}")

    # Clean up temp files
    cleanup_compile_result(result)

    return "\n".join(parts)


@mcp.tool()
async def run_pascal(
    source_code: str,
    compiler: str | None = None,
    stdin_input: str = "",
) -> str:
    """Compile and execute Pascal source code, returning the program output.

    Compiles the source code, runs the resulting executable, and returns
    both compilation messages and program output (stdout/stderr).

    Args:
        source_code: The complete Pascal source code to compile and run.
            Should be a program (not a unit) with a begin..end block.
        compiler: Which compiler to use. Can be a type name ('fpc', 'dcc32',
            'dcc64') or a full path to a specific compiler executable (e.g.,
            'C:\\Program Files (x86)\\Embarcadero\\Studio\\37.0\\bin\\dcc64.exe').
            If not specified, auto-selects the best available compiler.
        stdin_input: Optional text input to send to the program's stdin.
            Useful for programs that read from input.
    """
    result = run_source(
        source_code,
        compiler_type=compiler,
        stdin_input=stdin_input,
    )

    parts = [f"Compiler: {result.compiler_used}"]
    parts.append(f"Success: {result.success}")
    parts.append(f"Exit code: {result.exit_code}")

    if result.stdout.strip():
        parts.append(f"\n{result.stdout.strip()}")
    if result.stderr.strip():
        parts.append(f"\n--- Errors ---\n{result.stderr.strip()}")

    return "\n".join(parts)


@mcp.tool()
async def check_syntax(
    source_code: str,
    compiler: str | None = None,
) -> str:
    """Check Pascal syntax without producing an executable.

    Performs a syntax-only check (no linking). Faster than a full compile
    and useful for quickly validating code structure.

    Args:
        source_code: The Pascal source code to check.
        compiler: Which compiler to use. Can be a type name ('fpc', 'dcc32',
            'dcc64') or a full path to a specific compiler executable.
            If not specified, auto-selects the best available compiler.
    """
    result = compile_source(source_code, compiler_type=compiler, syntax_only=True)

    parts = [f"Compiler: {result.compiler_used}"]

    if result.success:
        parts.append("Syntax check: PASSED")
    else:
        parts.append("Syntax check: FAILED")

    if result.stdout.strip():
        parts.append(f"\n{result.stdout.strip()}")
    if result.stderr.strip():
        parts.append(f"\n{result.stderr.strip()}")

    # Clean up temp files
    cleanup_compile_result(result)

    return "\n".join(parts)


@mcp.tool()
async def parse_form(
    file_path: str,
    output_format: str = "tree",
) -> str:
    """Parse a Delphi/Lazarus form file and return its component structure.

    Reads .dfm (VCL), .fmx (FireMonkey), or .lfm (Lazarus) form files
    and returns a structured view of all components, their properties,
    positions, sizes, and event handlers.

    Args:
        file_path: Absolute path to the .dfm, .fmx, or .lfm file.
        output_format: How to format the output:
            - 'tree': Indented component tree with key properties (default)
            - 'summary': High-level overview with component counts and events
            - 'flat': Flat list of all components with position/size info
    """
    try:
        root = parse_form_file(file_path)
    except ValueError as e:
        return str(e)

    if root is None:
        return f"Could not parse form file: {file_path}"

    if output_format == "summary":
        return format_summary(root)
    elif output_format == "flat":
        return format_component_list(root)
    else:
        return format_tree(root)


@mcp.tool()
async def screenshot_app(
    window_title: str,
) -> list:
    """Take a screenshot of a running application window.

    Finds a window by its title (or partial title) and captures just
    that window as a PNG image without stealing focus or disrupting
    the user's desktop.
    Use list_app_windows first if you need to find the exact title.

    Args:
        window_title: Full or partial window title to capture (case-insensitive).
            For example: 'Hello World App' or just 'Hello'.
    """
    result = capture_window(window_title)

    if result is None:
        windows = list_windows(window_title)
        if windows:
            titles = "\n".join(f"  - {w['title']}" for w in windows[:10])
            return f"Window not found for '{window_title}'. Similar windows:\n{titles}"
        return (
            f"No window found matching '{window_title}'. "
            "Use the list_app_windows tool to see all open windows."
        )

    b64_data, actual_title, width, height = result
    return [
        Image(data=base64.b64decode(b64_data), format="png"),
        f"Screenshot of '{actual_title}' ({width}x{height})",
    ]


@mcp.tool()
async def list_app_windows(
    filter_text: str = "",
) -> str:
    """List visible application windows on the desktop.

    Use this to find the exact title of a window before taking
    a screenshot with screenshot_app.

    Args:
        filter_text: Optional text to filter window titles (case-insensitive).
            Leave empty to list all visible windows.
    """
    windows = list_windows(filter_text)

    if not windows:
        if filter_text:
            return f"No visible windows matching '{filter_text}'."
        return "No visible windows found."

    lines = [f"Found {len(windows)} window(s):\n"]
    for w in windows:
        lines.append(f"  {w['title']}")

    return "\n".join(lines)


@mcp.tool()
async def launch_app(
    source_code: str,
    compiler: str | None = None,
) -> str:
    """Compile Pascal source and launch the GUI application in background.

    Use this for GUI applications (VCL/FMX) that need to stay running
    so you can see and interact with them. Unlike run_pascal, this does
    not wait for the program to finish — it launches and returns immediately.

    After launching, use the preview system (preview_start with
    "pascal-preview") to see the running application, or use
    screenshot_app to capture a screenshot.

    Args:
        source_code: The complete Pascal source code to compile and launch.
            Should be a GUI program (VCL/FMX) with forms.
        compiler: Which compiler to use. Can be a type name ('fpc', 'dcc32',
            'dcc64') or a full path to a specific compiler executable.
            If not specified, auto-selects the best available compiler.
    """
    result = compile_and_launch(source_code, compiler_type=compiler)

    parts = [f"Success: {result.success}"]
    parts.append(result.message)

    if result.exe_path:
        parts.append(f"Executable: {result.exe_path}")

    if result.success:
        parts.append(
            "\nTo see the app, use preview_start('pascal-preview') or "
            "screenshot_app with the window title."
        )

    return "\n".join(parts)


@mcp.tool()
async def compile_delphi_project(
    project_name: str = "Project1",
    form_caption: str = "My Application",
    components: str = "[]",
    events: str = "[]",
    compiler: str | None = None,
    output_dir: str | None = None,
    project_type: str = "vcl",
    program_body: str = "",
) -> str:
    """Compile a Delphi project using proper templates (DPR + PAS + DFM).

    This is the ONLY correct way to build Delphi applications — do NOT
    use MSBuild, shell commands, or other build systems. This tool
    generates proper project structure and invokes the Delphi compiler
    (dcc32/dcc64) or Free Pascal directly.

    Args:
        project_name: Name for the project (e.g., 'HelloWorld').
        form_caption: Title bar text for the main form (VCL only).
        components: JSON array of components. Each component is an object:
            [{"type": "TButton", "name": "btnHello", "caption": "Say Hello",
              "left": 130, "top": 120, "width": 140, "height": 45,
              "event": "btnHelloClick"}]
            Supported types: TButton, TEdit, TLabel, TMemo.
        events: JSON array of event handlers:
            [{"name": "btnHelloClick", "body": "ShowMessage('Hello!');"}]
        compiler: Which compiler to use ('fpc', 'dcc32', 'dcc64', or full path).
        output_dir: Optional directory for output files. If not specified,
            uses a temp directory.
        project_type: One of:
            'vcl'     — Windows-only desktop GUI app (Vcl.Forms, .dfm).
            'fmx'     — FireMonkey cross-platform GUI app (FMX.Forms, .fmx).
                        Emits a .dproj wired for Win32 + Win64 + Android64
                        so build_dproj can cross-compile to mobile without
                        manual editing. Use this when the user wants
                        Android/iOS/macOS, or just "a mobile-ready app".
            'console' — text-mode program (no form).
            'fpc'     — Free Pascal program (cross-platform but no GUI).
        program_body: For console/fpc projects, the main program code.
    """
    import json

    try:
        comp_list = json.loads(components) if components else []
    except json.JSONDecodeError as e:
        return f"Invalid components JSON: {e}"

    try:
        evt_list = json.loads(events) if events else []
    except json.JSONDecodeError as e:
        return f"Invalid events JSON: {e}"

    # Generate project files from templates
    if project_type == "vcl":
        files = generate_vcl_project(
            project_name=project_name,
            form_caption=form_caption,
            components=comp_list,
            events=evt_list,
            compiler_type=compiler,
        )
    elif project_type == "fmx":
        # FMX gets a full .dproj alongside the .dpr/.pas/.fmx so build_dproj
        # can cross-compile to Android out of the box. The dproj targets
        # Win32/Win64/Android64; iOS/macOS can be added in RAD Studio later
        # (they need PAServer setup and the deploy-manifest synthesizer).
        files = generate_fmx_project(
            project_name=project_name,
            form_caption=form_caption,
            components=comp_list,
            events=evt_list,
            compiler_type=compiler,
        )
    elif project_type == "console":
        body = program_body or "    Writeln('Hello, World!');"
        files = generate_console_project(
            project_name=project_name,
            program_body=body,
            compiler_type=compiler,
        )
    elif project_type == "fpc":
        body = program_body or "  Writeln('Hello, World!');"
        files = generate_fpc_project(
            project_name=project_name,
            program_body=body,
        )
    else:
        return (
            f"Unknown project_type: {project_type}. "
            "Use 'vcl' (Windows desktop), 'fmx' (cross-platform incl. mobile), "
            "'console' (text-mode), or 'fpc' (Free Pascal)."
        )

    # Show what was generated
    parts = [f"Generated {len(files)} file(s):"]
    for fname in files:
        parts.append(f"  - {fname}")

    # Compile the project
    result = compile_project(files, compiler_type=compiler, output_dir=output_dir)

    parts.append(f"\nCompiler: {result.compiler_used}")
    parts.append(f"Success: {result.success}")

    if result.stdout.strip():
        parts.append(f"\n--- Compiler Output ---\n{result.stdout.strip()}")
    if result.stderr.strip():
        parts.append(f"\n--- Compiler Messages ---\n{result.stderr.strip()}")

    if result.exe_path:
        parts.append(f"\nExecutable: {result.exe_path}")

    if not output_dir and result.exe_path:
        parts.append("\nNote: Files are in a temp directory. Use output_dir to save permanently.")

    return "\n".join(parts)


@mcp.tool()
async def build_dproj(
    dproj_path: str,
    config: str = "Debug",
    platform: str = "Win32",
    target: str = "Build",
    studio_root: str | None = None,
    timeout: int = 600,
    deep_clean: bool | None = None,
    remote_profile: str | None = None,
    deploy: bool | None = None,
    synthesize_ios_manifest: bool = False,
) -> str:
    """Build an existing Delphi .dproj project file using MSBuild + rsvars.bat.

    Use this for real-world multi-file Delphi projects (CuttlefishV2.dproj,
    etc.) — anything that already exists on disk with its own .dproj, .dpr,
    units, forms, search paths, conditional defines, and resources.
    Honours the project's full build configuration exactly as RAD Studio
    would, no template substitution.

    NOTE: compile_delphi_project is for *generating* a new throwaway project
    from a TButton/TEdit/TLabel/TMemo template. build_dproj is for building
    an existing real project. Use the right one.

    STAGING CLEAN (Android / iOS / macOS / Linux): Delphi's own MSBuild
    Clean/Rebuild targets do NOT fully clean staging-based build pipelines.
    They wipe DCU/.o files but leave the PAClient (Android) or PAServer
    (iOS/macOS/Linux) staging directory and the previous artifact in place.
    That causes the classic "I changed code/assets but the new APK/app
    didn't update" symptom. When platform is Android/iOS/macOS/Linux and
    target is Rebuild or Clean, this tool automatically deep-cleans the
    platform's intermediate and bin directories before invoking MSBuild.
    Pass deep_clean=False to disable, or deep_clean=True to force it.

    OUTPUT PATHS: this tool reads the .dproj's own DCC_ExeOutput / DCC_DcuOutput
    / DCC_BplOutput properties (resolved by MSBuild, honouring conditional
    PropertyGroups, $(Platform)/$(Config) substitution, base config inheritance,
    etc.) and uses those for both deep_clean and artifact resolution. So if your
    dproj points outputs at ..\\bin\\$(Platform)\\$(Config) or anywhere else
    non-default, this tool follows. Safe guard: paths resolving outside the
    project tree (e.g. the shared C:\\Users\\Public\\...\\Bpl\\ system dir) are
    NEVER deep-cleaned; the trace will list them as "Skipped".

    PASERVER (iOS / macOS / Linux): cross-builds for these platforms run
    through PAServer on a remote Mac or Linux host. Pass remote_profile
    with the name of a Connection Profile already configured in RAD Studio
    (Tools → Options → Environment Options → Connection Profile Manager).
    PAServer must be running on the target host. If the .dproj already
    pins a default profile you can omit remote_profile, but explicit is
    safer. This tool does not create profiles or store credentials —
    the profile must already exist locally on the dev machine.

    Args:
        dproj_path: Absolute path to the .dproj file
            (e.g. r"D:\\projects\\cuttlefishmobile\\src\\CuttlefishV2.dproj").
        config: Build config — Debug, Release, etc. (default Debug).
        platform: Target platform — Win32, Win64, Android64, iOSDevice64,
            iOSSimARM64, OSX64, OSXARM64, Linux64 (default Win32). For
            Cuttlefish always use Win32 unless explicitly building for mobile.
        target: MSBuild target — Build (default), Rebuild (clean+build), or Clean.
        studio_root: Optional Studio install (e.g. r"C:\\Program Files (x86)\\Embarcadero\\Studio\\37.0").
            Defaults to the highest-version install detected.
        timeout: Seconds before the build is killed (default 600). Remote
            iOS/macOS/Linux builds can be slow on first run — bump to 1800+
            if PAServer needs to re-deploy a large bundle.
        deep_clean: Nuke the platform's intermediate + bin dirs before building.
            None (default) auto-enables for Android/iOS/macOS/Linux Rebuild
            or Clean. True forces it on for any platform. False disables it.
        remote_profile: Name of the RAD Studio Connection Profile for PAServer.
            Required for iOS/macOS/Linux unless the .dproj pins a default.
            Ignored for Win32/Win64/Android. Example: "MyMacMini". If omitted
            on a PAServer platform, the tool auto-selects from registered
            Connection Profiles when exactly one is compatible — multiple
            matches force an explicit choice for safety (so a Linux Debug
            build can't accidentally hit a "production" PAServer host).
        deploy: Chain MSBuild's /t:Deploy after the requested target.
            Required to produce a packaged artifact on Android (APK), iOS
            (.app bundle + codesign), macOS (.app), and Linux (binary
            staged on remote). None (default) auto-enables for those
            platforms whenever target isn't Clean. False keeps the legacy
            "compile and link only" behaviour. Ignored for Win32/Win64.
        synthesize_ios_manifest: For iOS targets only. The IDE writes 4
            DeployFile entries per Config × Platform on first deploy
            (Entitlements, InfoPList, LaunchScreen, ProjectOutput). Command-
            line Deploy can't synthesize them, so projects never IDE-deployed
            to iOS fail with cryptic "codesign … No such file" errors.
            Setting this True auto-adds the missing entries to the .dproj
            before Deploy runs, after writing a timestamped .bak backup.
            Default False — the build trace will report what's missing
            without mutating anything. Use check_ios_deploy to inspect
            without building.
    """
    result = build_existing_dproj(
        dproj_path=dproj_path,
        config=config,
        platform=platform,
        target=target,
        studio_root=studio_root,
        timeout=timeout,
        deep_clean=deep_clean,
        remote_profile=remote_profile,
        deploy=deploy,
        synthesize_ios_manifest=synthesize_ios_manifest,
    )

    parts = [
        f"Project: {dproj_path}",
        f"Compiler: {result.compiler_used}",
        f"Target/Config/Platform: {target} / {config} / {platform}",
        f"Exit code: {result.exit_code}",
        f"Success: {result.success}",
    ]
    if result.exe_path:
        parts.append(f"Artifact: {result.exe_path}")

    # Trim noisy MSBuild output to the interesting lines
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if out:
        # Keep last ~80 lines so error context shows even on large logs
        lines = out.splitlines()
        if len(lines) > 80:
            lines = ["... (truncated, showing last 80 lines) ..."] + lines[-80:]
        parts.append("\n--- MSBuild Output ---\n" + "\n".join(lines))
    if err:
        parts.append("\n--- MSBuild Stderr ---\n" + err)

    return "\n".join(parts)


@mcp.tool()
async def check_ios_deploy(
    dproj_path: str,
    config: str = "Debug",
    platform: str = "iOSDevice64",
) -> str:
    """Check whether a .dproj has the iOS DeployFile entries required for Deploy.

    PAServer's iOS Deploy target reads the dproj's <Deployment> section to
    decide what to ship to the Mac for codesign + .app assembly. The IDE
    writes 4 entries per Config × Platform on first deploy:
    ProjectiOSEntitlements, ProjectiOSInfoPList, ProjectiOSLaunchScreen,
    ProjectOutput. If they're missing — common when a project was renamed
    or never IDE-deployed to a given target — Deploy ships nothing and
    codesign fails with "<Proj>.app: No such file or directory".

    This tool only INSPECTS; it never mutates. Use synthesize_ios_manifest=True
    on build_dproj to add missing entries (with .bak backup).

    Args:
        dproj_path: Absolute path to the .dproj.
        config: Build configuration to check (Debug, Release, etc.).
        platform: iOS platform: iOSDevice64 or iOSSimARM64.
    """
    import os as _os
    from pascal_mcp.iosdeploy import detect_ios_deploy_entries
    if not _os.path.isfile(dproj_path):
        return f"dproj not found: {dproj_path}"
    project_name = _os.path.splitext(_os.path.basename(dproj_path))[0]
    status = detect_ios_deploy_entries(dproj_path, config, platform, project_name)
    lines = [
        f"Project:  {project_name}",
        f"Config:   {config}",
        f"Platform: {platform}",
        f"Ready:    {status.ready_to_deploy}",
        f"Present:  {', '.join(status.present_classes) or '(none)'}",
        f"Missing:  {', '.join(status.missing_classes) or '(none)'}",
        "",
        status.summary(),
    ]
    return "\n".join(lines)


@mcp.tool()
async def paserver_info(profile: str, studio_root: str | None = None) -> str:
    """Read local info about a PAServer Connection Profile (paclient -l).

    Pure local read — does NOT touch the network. Returns the host, port,
    platform tag, and sysroot path the IDE has stored for this profile.
    The profile name must already exist in HKCU registry (use
    list_remote_profiles to enumerate). paclient itself doesn't error on
    unknown names — it returns synthesised defaults — so we validate
    against the registry first to catch typos with a clear error.

    Args:
        profile: Connection Profile name (e.g. "MACBOOK"). Case-sensitive.
        studio_root: Optional Studio install root. Defaults to the
            highest-version detected.
    """
    from pascal_mcp.paclient import find_paclient, get_paserver_info
    pc = find_paclient(studio_root)
    if pc is None:
        return (
            "paclient.exe not found. Expected at "
            "<studio_root>\\bin\\paclient.exe. Is RAD Studio installed?"
        )
    version = None
    if studio_root:
        version = _studio_version_from_root(studio_root)
    info = get_paserver_info(pc, profile, studio_version=version)
    if info is None:
        return (
            f"Profile '{profile}' is not registered in HKCU\\Software\\Embarcadero"
            f"\\BDS\\<ver>\\RemoteProfiles. Use list_remote_profiles to see what "
            "is available, or create one in Connection Profile Manager."
        )
    return (
        f"Profile:   {info.profile}\n"
        f"Location:  {info.location}\n"
        f"Platform:  {info.platform}\n"
        f"Host:      {info.host}:{info.port}\n"
        f"Sysroot:   {info.sysroot}\n"
        "(Password is the registry-encrypted form; not echoed.)"
    )


@mcp.tool()
async def paserver_check_connection(
    profile: str,
    timeout: float = 3.0,
    studio_root: str | None = None,
) -> str:
    """Two-stage reachability check for a PAServer Connection Profile.

    Runs:
      1. Registry/paclient lookup to confirm the profile exists locally
         and resolve its host:port.
      2. A plain TCP connect to that host:port to confirm PAServer is
         listening.

    Doesn't perform a full PAServer protocol handshake (that requires the
    paclient password and isn't necessary to answer "is the host even
    reachable"). For full validation, run any file-transfer tool against
    the profile — paclient will surface protocol-level errors.

    Use this as the first pre-flight step before iOS/macOS/Linux builds
    or before paserver_get/paserver_put. Answers ~90% of "why is my
    PAServer build failing" questions immediately.

    Args:
        profile: Connection Profile name (e.g. "MACBOOK").
        timeout: TCP connect timeout in seconds. Default 3.
        studio_root: Optional Studio install root.
    """
    from pascal_mcp.paclient import find_paclient, check_paserver_connection
    pc = find_paclient(studio_root)
    if pc is None:
        return "paclient.exe not found under any RAD Studio install."
    result = check_paserver_connection(pc, profile, timeout=timeout)
    lines = [
        f"Profile: {result.profile}",
        f"Host:    {result.host}:{result.port}",
        f"profile_ok: {result.profile_ok}",
        f"tcp_ok:     {result.tcp_ok}",
        "",
    ]
    lines.extend(f"  - {n}" for n in result.notes)
    return "\n".join(lines)


@mcp.tool()
async def paserver_scratch_dir(profile: str, remote_user: str) -> str:
    """Return PAServer's per-profile scratch directory path on the remote host.

    PAServer in restricted mode (the default) only allows file ops inside
    /Users/<remote_user>/PAServer/scratch-dir/<windows_user>-<PROFILE>/.
    Use this helper to get the right path before calling paserver_put /
    paserver_get, or to clean up after a build.

    Args:
        profile: Connection Profile name (e.g. "MACBOOK").
        remote_user: Unix username running paserver on the Mac/Linux host.
            You can find this by SSHing to the host once, or check the
            PAServer terminal output for the home dir.
    """
    from pascal_mcp.paclient import paserver_scratch_dir as compose
    return compose(profile, remote_user)


@mcp.tool()
async def paserver_get(
    profile: str,
    remote_path: str,
    local_dir: str,
    timeout: int = 300,
    studio_root: str | None = None,
) -> str:
    """Pull a file or directory from the PAServer remote host to this box.

    Wraps `paclient -g <remote>,<local_dir>`. The remote_path can use
    PAClient's wildcard syntax (e.g. ``Documents/logs/*.txt``). Useful for
    pulling crash logs, build artifacts the IDE assembled on the remote
    (codesigned .app bundles, .ipa files), or any other file you need
    without setting up a separate SSH layer.

    Args:
        profile: Connection Profile name.
        remote_path: Path on the PAServer host. May contain wildcards.
        local_dir: Local directory to drop the files into. Created if
            missing.
        timeout: Seconds before the transfer is killed (default 300).
        studio_root: Optional Studio install root.
    """
    from pascal_mcp.paclient import find_paclient, paserver_get as do_get
    pc = find_paclient(studio_root)
    if pc is None:
        return "paclient.exe not found."
    r = do_get(pc, profile, remote_path, local_dir, timeout=timeout)
    head = (
        f"profile: {r.profile}\noperation: {r.operation}\n"
        f"success: {r.success}\n\n"
    )
    body = ""
    if r.output:
        body += "--- paclient output ---\n" + r.output.strip() + "\n"
    if r.errors:
        body += "--- paclient stderr ---\n" + r.errors.strip() + "\n"
    return head + body


@mcp.tool()
async def paserver_put(
    profile: str,
    local_path: str,
    remote_dir: str,
    timeout: int = 300,
    studio_root: str | None = None,
) -> str:
    """Push a file or directory from this box to the PAServer remote host.

    Wraps `paclient -u <local>,<remote_dir>`. The local_path can use
    wildcard syntax (e.g. ``build\\\\*.so``). Useful for staging files for
    a manual remote build, replacing assets the IDE didn't deploy, or
    seeding the PAServer scratch directory.

    Args:
        profile: Connection Profile name.
        local_path: File or directory on this Windows box. May contain
            wildcards.
        remote_dir: Destination directory on the PAServer host.
        timeout: Seconds before the transfer is killed (default 300).
        studio_root: Optional Studio install root.
    """
    from pascal_mcp.paclient import find_paclient, paserver_put as do_put
    pc = find_paclient(studio_root)
    if pc is None:
        return "paclient.exe not found."
    r = do_put(pc, profile, local_path, remote_dir, timeout=timeout)
    head = (
        f"profile: {r.profile}\noperation: {r.operation}\n"
        f"success: {r.success}\n\n"
    )
    body = ""
    if r.output:
        body += "--- paclient output ---\n" + r.output.strip() + "\n"
    if r.errors:
        body += "--- paclient stderr ---\n" + r.errors.strip() + "\n"
    return head + body


@mcp.tool()
async def mac_ssh_check(host: str, user: str, key_path: str | None = None) -> str:
    """Test SSH connectivity + key auth to the remote Mac.

    Runs `whoami` over SSH with BatchMode (no interactive prompts) and
    verifies we land on the expected account. If key auth isn't set up,
    returns the exact ssh-copy-id command to fix it.

    Args:
        host: Mac hostname or IP (use the same address as the PAServer
            profile's Host field — see paserver_info).
        user: Mac user account.
        key_path: Optional explicit SSH private key path.
    """
    from pascal_mcp.mac_ssh import ssh_check
    ok, msg = ssh_check(host, user, key_path=key_path)
    return ("OK\n" if ok else "FAIL\n") + msg


@mcp.tool()
async def mac_ssh_run(
    host: str, user: str, command: str,
    key_path: str | None = None, timeout: int = 60,
) -> str:
    """Run an arbitrary command on the remote Mac via SSH.

    The building block for any Mac-side operation that paclient.exe doesn't
    cover (xcrun simctl, devicectl, log inspection, log streaming, etc.).
    Most callers should prefer the sim_* tools that wrap simctl directly;
    use this when you need something specific that doesn't have a wrapper.

    Args:
        host: Mac hostname or IP.
        user: Mac user account.
        command: Single command line. Quote internal spaces yourself.
        key_path: Optional explicit SSH private key path.
        timeout: Seconds before killed.
    """
    from pascal_mcp.mac_ssh import ssh_run
    return ssh_run(host, user, command, key_path=key_path, timeout=timeout).summarise()


@mcp.tool()
async def sim_list(
    host: str, user: str, booted_only: bool = False,
    key_path: str | None = None,
) -> str:
    """List iOS simulators on the Mac (xcrun simctl list devices --json).

    Returns the raw simctl JSON so the caller can pick UDIDs / runtime
    versions / names. Pass booted_only=True to filter to currently-running.
    """
    from pascal_mcp.ios_sim import sim_list as do_list
    return do_list(host, user, booted_only=booted_only, key_path=key_path).summarise()


@mcp.tool()
async def sim_boot(host: str, user: str, udid: str, key_path: str | None = None) -> str:
    """Boot a simulator by UDID (xcrun simctl boot)."""
    from pascal_mcp.ios_sim import sim_boot as do_boot
    return do_boot(host, user, udid, key_path=key_path).summarise()


@mcp.tool()
async def sim_shutdown(
    host: str, user: str, udid: str = "booted",
    key_path: str | None = None,
) -> str:
    """Shut down a simulator. Pass udid='booted' to stop all running ones."""
    from pascal_mcp.ios_sim import sim_shutdown as do_shutdown
    return do_shutdown(host, user, udid, key_path=key_path).summarise()


@mcp.tool()
async def sim_install(
    host: str, user: str, app_path: str, udid: str = "booted",
    key_path: str | None = None,
) -> str:
    """Install a .app bundle on a simulator (xcrun simctl install).

    app_path is the path on the Mac. After a build_dproj for iOSSimARM64
    with deploy chained, the .app lives in PAServer's scratch dir —
    use paserver_scratch_dir + the project name to compose it.
    """
    from pascal_mcp.ios_sim import sim_install as do_install
    return do_install(host, user, app_path, udid=udid, key_path=key_path).summarise()


@mcp.tool()
async def sim_launch(
    host: str, user: str, bundle_id: str, udid: str = "booted",
    key_path: str | None = None,
) -> str:
    """Launch an installed app by bundle identifier (xcrun simctl launch)."""
    from pascal_mcp.ios_sim import sim_launch as do_launch
    return do_launch(host, user, bundle_id, udid=udid, key_path=key_path).summarise()


@mcp.tool()
async def sim_terminate(
    host: str, user: str, bundle_id: str, udid: str = "booted",
    key_path: str | None = None,
) -> str:
    """Terminate a running app by bundle identifier."""
    from pascal_mcp.ios_sim import sim_terminate as do_terminate
    return do_terminate(host, user, bundle_id, udid=udid, key_path=key_path).summarise()


@mcp.tool()
async def sim_uninstall(
    host: str, user: str, bundle_id: str, udid: str = "booted",
    key_path: str | None = None,
) -> str:
    """Uninstall an app from a simulator."""
    from pascal_mcp.ios_sim import sim_uninstall as do_uninstall
    return do_uninstall(host, user, bundle_id, udid=udid, key_path=key_path).summarise()


@mcp.tool()
async def sim_open_url(
    host: str, user: str, url: str, udid: str = "booted",
    key_path: str | None = None,
) -> str:
    """Open a URL in the simulator (deep link or web URL)."""
    from pascal_mcp.ios_sim import sim_open_url as do_open
    return do_open(host, user, url, udid=udid, key_path=key_path).summarise()


@mcp.tool()
async def sim_screenshot(
    host: str, user: str, udid: str = "booted",
    key_path: str | None = None,
) -> list:
    """Capture a simulator screenshot and return it as an Image (parity with adb_screenshot).

    Pipes through base64 over SSH so we don't need a separate scp step.
    Returns [Image, description] on success, or an error string on failure.
    """
    import base64 as _b64
    from pascal_mcp.ios_sim import sim_screenshot_b64
    result = sim_screenshot_b64(host, user, udid=udid, key_path=key_path)
    if not result.success:
        return result.summarise()
    try:
        png = _b64.b64decode(result.stdout)
    except Exception as e:
        return f"sim_screenshot succeeded but base64 decode failed: {e}"
    return [
        Image(data=png, format="png"),
        f"Simulator screenshot ({len(png)} bytes PNG)",
    ]


@mcp.tool()
async def ios_codesign(
    profile: str,
    app_path: str,
    certificate: str,
    entitlement: str | None = None,
    notarize: bool = False,
    timeout: int = 300,
    studio_root: str | None = None,
) -> str:
    """Codesign an iOS/macOS .app bundle on the remote Mac (paclient -c).

    The .app must already exist on the Mac (typically in PAServer's scratch
    dir after build_dproj + Deploy ran). The certificate is whatever's in
    the Mac's keychain — pass the common name (e.g. "iPhone Developer:
    Jane Doe (ABCDE12345)") or "-" for ad-hoc dash-signing (development
    use only; won't install on real devices). Notarization options need
    Apple Developer Program membership + notarytool setup on the Mac.

    Args:
        profile: Connection Profile name.
        app_path: Remote path to the .app bundle.
        certificate: Identity in the Mac's keychain, or "-" for ad-hoc.
        entitlement: Optional remote path to entitlements.plist.
        notarize: Apply notarization (requires entitlement).
        timeout: Seconds before killed.
        studio_root: Optional Studio install root.
    """
    from pascal_mcp.paclient import find_paclient, ios_codesign as do_codesign
    pc = find_paclient(studio_root)
    if pc is None:
        return "paclient.exe not found."
    r = do_codesign(
        pc, profile, app_path, certificate,
        entitlement=entitlement, notarize=notarize, timeout=timeout,
    )
    head = (
        f"profile: {r.profile}\noperation: {r.operation}\n"
        f"success: {r.success}\n"
    )
    if r.output_path:
        head += f"signed: {r.output_path}\n"
    head += "\n"
    body = ""
    if r.output:
        body += "--- paclient output ---\n" + r.output.strip() + "\n"
    if r.errors:
        body += "--- paclient stderr ---\n" + r.errors.strip() + "\n"
    return head + body


@mcp.tool()
async def ios_create_ipa(
    profile: str,
    app_path: str,
    out_path: str,
    certificate: str,
    provisioning_profile: str,
    ipa_type: int = 1,
    timeout: int = 600,
    studio_root: str | None = None,
) -> str:
    """Package a signed .app into an .ipa on the remote Mac (paclient -i).

    The .app must be codesigned first (ios_codesign). The provisioning
    profile must exist on the Mac and match the cert + bundle ID. IPA
    assembly runs entirely on the Mac via xcrun.

    Args:
        profile: Connection Profile name.
        app_path: Remote path to the signed .app.
        out_path: Remote path where the .ipa should be written.
        certificate: Same identity used for codesign.
        provisioning_profile: Remote path to a .mobileprovision file.
        ipa_type: 1 = ad-hoc / dev distribution, 2 = App Store. Default 1.
        timeout: Seconds before killed (default 600).
        studio_root: Optional Studio install root.
    """
    from pascal_mcp.paclient import find_paclient, ios_create_ipa as do_ipa
    pc = find_paclient(studio_root)
    if pc is None:
        return "paclient.exe not found."
    r = do_ipa(
        pc, profile, app_path, out_path, certificate,
        provisioning_profile, ipa_type=ipa_type, timeout=timeout,
    )
    head = (
        f"profile: {r.profile}\noperation: {r.operation}\n"
        f"success: {r.success}\n"
    )
    if r.output_path:
        head += f"ipa: {r.output_path}\n"
    head += "\n"
    body = ""
    if r.output:
        body += "--- paclient output ---\n" + r.output.strip() + "\n"
    if r.errors:
        body += "--- paclient stderr ---\n" + r.errors.strip() + "\n"
    return head + body


@mcp.tool()
async def ios_install_ipa(
    profile: str,
    ipa_path: str,
    device_udid: str,
    timeout: int = 300,
    studio_root: str | None = None,
) -> str:
    """Install an .ipa on an iOS device attached to the Mac (paclient -ii).

    The iOS device must be physically connected to the Mac, trusted, and
    listed by xcrun devicectl. Find the UDID via `idevice_id -l` on the
    Mac, or in Xcode → Window → Devices and Simulators.

    Note: this is for *device* installation. Simulator installation needs
    xcrun simctl install which paclient does NOT wrap — that's tracked
    under issue #5 (sim_* tools).

    Args:
        profile: Connection Profile name.
        ipa_path: Remote path to the .ipa on the Mac.
        device_udid: Target iOS device UDID.
        timeout: Seconds before killed.
        studio_root: Optional Studio install root.
    """
    from pascal_mcp.paclient import find_paclient, ios_install_ipa as do_install
    pc = find_paclient(studio_root)
    if pc is None:
        return "paclient.exe not found."
    r = do_install(pc, profile, ipa_path, device_udid, timeout=timeout)
    head = (
        f"profile: {r.profile}\noperation: {r.operation}\n"
        f"success: {r.success}\n\n"
    )
    body = ""
    if r.output:
        body += "--- paclient output ---\n" + r.output.strip() + "\n"
    if r.errors:
        body += "--- paclient stderr ---\n" + r.errors.strip() + "\n"
    return head + body


@mcp.tool()
async def paserver_remove(
    profile: str,
    remote_path: str,
    timeout: int = 60,
    studio_root: str | None = None,
) -> str:
    """Delete a file or directory on the PAServer remote host.

    Wraps `paclient -R <remote_path>` (capital-R: removes from the *remote*
    host, not the local cache). Use to clean up scratch dirs or old
    deployments. Be careful with wildcards.

    Args:
        profile: Connection Profile name.
        remote_path: Path on the PAServer host. Wildcards allowed.
        timeout: Seconds before the operation is killed (default 60).
        studio_root: Optional Studio install root.
    """
    from pascal_mcp.paclient import find_paclient, paserver_remove as do_remove
    pc = find_paclient(studio_root)
    if pc is None:
        return "paclient.exe not found."
    r = do_remove(pc, profile, remote_path, timeout=timeout)
    head = (
        f"profile: {r.profile}\noperation: {r.operation}\n"
        f"success: {r.success}\n\n"
    )
    body = ""
    if r.output:
        body += "--- paclient output ---\n" + r.output.strip() + "\n"
    if r.errors:
        body += "--- paclient stderr ---\n" + r.errors.strip() + "\n"
    return head + body


@mcp.tool()
async def list_remote_profiles(studio_root: str | None = None) -> str:
    """List PAServer Connection Profiles registered in this RAD Studio install.

    These profiles drive iOS / macOS / Linux builds via PAServer on a remote
    Mac or Linux host. build_dproj uses them automatically (it picks the first
    compatible profile for the target platform), but listing them is useful
    when troubleshooting a "Missing profile name" or "No remote profile"
    error from MSBuild Deploy.

    Each profile lists:
      - Name (what you pass as remote_profile=)
      - Platform tag (OSX64, iOSDevice64, Linux64 — used to filter for the
        target. An OSX64 profile is reusable for any Apple target.)
      - Host:port (where PAServer is listening)
      - Whether the sidecar .profile file exists at
        %APPDATA%\\Embarcadero\\BDS\\<ver>\\<name>.profile. The sidecar is
        REQUIRED for Deploy to read the profile — if missing, open the
        profile in Connection Profile Manager once to write it.

    Args:
        studio_root: Optional RAD Studio install root (e.g.
            r"C:\\Program Files (x86)\\Embarcadero\\Studio\\37.0"). Defaults
            to the highest-version install detected.
    """
    if studio_root is None:
        roots = _discover_studio_roots()
        if not roots:
            return "No RAD Studio installation found."
        studio_root = roots[0]

    version = _studio_version_from_root(studio_root)
    if version is None:
        return f"Could not determine Studio version from path: {studio_root}"

    profiles = _discover_remote_profiles(version)
    if not profiles:
        return (
            f"No PAServer profiles registered under HKCU\\Software\\Embarcadero"
            f"\\BDS\\{version}\\RemoteProfiles.\n\n"
            "Open RAD Studio → Tools → Options → Environment Options → "
            "Connection Profile Manager to add one."
        )

    lines = [f"RAD Studio {version} — {len(profiles)} connection profile(s):", ""]
    for p in profiles:
        sidecar = "OK" if p.profile_file_exists else "MISSING — Deploy will fail"
        lines.append(f"  {p.name}")
        lines.append(f"    Platform tag: {p.platform}")
        lines.append(f"    Host:         {p.hostname}:{p.port}")
        lines.append(f"    Sidecar file: {sidecar}")
        lines.append("")
    return "\n".join(lines).rstrip()


@mcp.tool()
async def setup_fpc(
    install_dir: str = r"C:\FPC\3.2.2",
) -> str:
    """Download and install Free Pascal Compiler (FPC).

    Only use this when no Pascal compiler is available on the system.
    Downloads FPC 3.2.2 from the official SourceForge mirror and performs
    a silent installation. May require administrator privileges.

    Args:
        install_dir: Where to install FPC. Defaults to C:\\FPC\\3.2.2.
            Avoid paths with spaces.
    """
    result = await download_and_install_fpc(install_dir)

    parts = [f"Status: {result['status']}"]
    parts.append(result["message"])

    if "version" in result:
        parts.append(f"Version: {result['version']}")
    if "path" in result:
        parts.append(f"Path: {result['path']}")

    return "\n".join(parts)


@mcp.tool()
async def focus_ide() -> str:
    """Restore the Delphi/Lazarus IDE window and bring it to the foreground.

    Finds a running RAD Studio / Delphi / Lazarus IDE and:
      1. If minimized, calls ShowWindow(SW_RESTORE) to un-minimize it.
      2. Calls SetForegroundWindow to make it the active window.

    This is the precondition for many IDE-driven workflows: observe_ide's
    screenshot is more reliable on a foreground window (the Windows
    Graphics Capture path can't reach GPU-composited Skia panels behind
    other windows), and any UI automation needs the window visible. Call
    this first whenever you're about to interact with the IDE.

    Returns a one-line confirmation with the window title, or a clear
    error if no IDE is running.
    """
    from pascal_mcp.screenshot import _bring_window_to_front
    ide = find_ide_window()
    if ide is None:
        return (
            "No Delphi/Lazarus IDE window found. Is RAD Studio running? "
            "If it is, the window may have an unrecognised title — check "
            "with list_app_windows."
        )
    try:
        _bring_window_to_front(ide["hwnd"])
    except Exception as e:  # pragma: no cover - win32 surface
        return f"Found IDE window '{ide['title']}' but failed to focus it: {e}"
    return f"Focused IDE window: {ide['title']} (hwnd={ide['hwnd']})"


@mcp.tool()
async def observe_ide(
    project_dir: str | None = None,
) -> list:
    """Observe the Delphi/Lazarus IDE and return a screenshot plus project info.

    Finds a running RAD Studio, Delphi, or Lazarus IDE window, captures
    a screenshot of it, and optionally scans the project directory for
    source files. Claude reads the screenshot to spot compiler errors,
    warnings, or other messages in the IDE's Messages pane.

    Args:
        project_dir: Optional path to the project directory on disk.
            If provided, also returns a list of project source files.
    """
    ide = find_ide_window()
    if ide is None:
        return "No Delphi/Lazarus IDE window found. Is RAD Studio running?"

    img = capture_ide_screenshot(ide["hwnd"])
    if img is None:
        return f"Found IDE window '{ide['title']}' but failed to capture screenshot."

    import io
    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    png_data = buffer.getvalue()

    result = [
        Image(data=png_data, format="png"),
        f"IDE: {ide['title']}",
    ]

    if ide["project_name"]:
        result.append(f"Project: {ide['project_name']}")

    if project_dir:
        files = find_project_files(project_dir)
        if "error" in files:
            result.append(f"Project scan: {files['error']}")
        else:
            summary = []
            for key in ["pas_files", "dfm_files", "fmx_files", "dpr_files", "dproj_files"]:
                count = len(files.get(key, []))
                if count:
                    summary.append(f"{count} {key.replace('_files', '').upper()}")
            if summary:
                result.append(f"Project files: {', '.join(summary)}")
            if files.get("units_from_dproj"):
                result.append(f"Units in .dproj: {', '.join(files['units_from_dproj'])}")

    return result


@mcp.tool()
async def read_ide_errors(
    project_dir: str,
    errors: str,
) -> str:
    """Read source code context around compiler error locations.

    After spotting errors in an IDE screenshot, call this tool with the
    parsed error locations to get the source code around each error.

    Args:
        project_dir: Path to the project directory on disk.
        errors: JSON array of error locations. Each entry is an object
            with 'file' and 'line' keys, e.g.:
            [{"file": "Unit1.pas", "line": 42},
             {"file": "MainForm.pas", "line": 15}]
    """
    import json

    try:
        error_list = json.loads(errors) if errors else []
    except json.JSONDecodeError as e:
        return f"Invalid errors JSON: {e}"

    if not error_list:
        return "No errors provided."

    # Get search paths from project
    project_info = find_project_files(project_dir)
    search_paths = project_info.get("search_paths", [])

    parts = []
    for err in error_list:
        filename = err.get("file", "")
        line = err.get("line", 0)

        if not filename:
            parts.append("Skipped entry with no filename.")
            continue

        resolved = resolve_error_file(filename, project_dir, search_paths)
        if resolved is None:
            parts.append(f"Could not find file: {filename}")
            continue

        context = read_source_context(resolved, line)
        parts.append(context)

    return "\n\n".join(parts)


@mcp.tool()
async def list_project_files(
    project_dir: str,
) -> str:
    """List all source files in a Delphi/Lazarus project directory.

    Scans the directory for Pascal source files (.pas, .dpr, .lpr),
    form files (.dfm, .fmx, .lfm), and project files (.dproj, .lpi).
    Also parses .dproj files for unit references, search paths, and
    build configuration.

    Args:
        project_dir: Path to the project directory on disk.
    """
    files = find_project_files(project_dir)

    if "error" in files:
        return files["error"]

    parts = [f"Project directory: {files['project_dir']}\n"]

    categories = [
        ("DPR (Delphi project)", "dpr_files"),
        ("DPROJ (MSBuild project)", "dproj_files"),
        ("LPR (Lazarus project)", "lpr_files"),
        ("LPI (Lazarus project info)", "lpi_files"),
        ("PAS (Pascal units)", "pas_files"),
        ("DFM (VCL forms)", "dfm_files"),
        ("FMX (FireMonkey forms)", "fmx_files"),
        ("LFM (Lazarus forms)", "lfm_files"),
    ]

    for label, key in categories:
        file_list = files.get(key, [])
        if file_list:
            parts.append(f"{label}: {len(file_list)}")
            for f in file_list:
                parts.append(f"  - {f}")
            parts.append("")

    if files.get("units_from_dproj"):
        parts.append(f"Units referenced in .dproj:")
        for u in files["units_from_dproj"]:
            parts.append(f"  - {u}")
        parts.append("")

    if files.get("search_paths"):
        parts.append(f"Search paths:")
        for sp in files["search_paths"]:
            parts.append(f"  - {sp}")
        parts.append("")

    if files.get("build_config"):
        parts.append(f"Active build config: {files['build_config']}")

    total = sum(len(files.get(k, [])) for _, k in categories)
    if total == 0:
        parts.append("No Pascal source files found in this directory.")

    return "\n".join(parts)


# --- Windows App Interaction Tools ---


@mcp.tool()
async def app_click(
    window_title: str,
    x: int,
    y: int,
    button: str = "left",
    double_click: bool = False,
) -> str:
    """Click on a Windows application window at the given coordinates.

    Coordinates use screenshot pixels — take a screenshot_app first to
    identify where to click, then use those pixel coordinates here.

    Uses PostMessage with automatic child window targeting so clicks
    reach the correct control (buttons, edits, etc.).

    Args:
        window_title: Full or partial window title (case-insensitive).
        x: X coordinate in screenshot pixels.
        y: Y coordinate in screenshot pixels.
        button: 'left' (default) or 'right'.
        double_click: If True, send a double-click.
    """
    try:
        return click_window(window_title, x, y, button=button, double=double_click)
    except RuntimeError as e:
        return str(e)


@mcp.tool()
async def app_type(window_title: str, text: str) -> str:
    """Type text into a Windows application window.

    Sends Unicode characters to the window's currently focused control.
    Click on a text field first with app_click to focus it.

    Args:
        window_title: Full or partial window title (case-insensitive).
        text: The text to type.
    """
    try:
        return type_in_window(window_title, text)
    except RuntimeError as e:
        return str(e)


@mcp.tool()
async def app_key(window_title: str, key: str) -> str:
    """Send a key or keyboard shortcut to a Windows application window.

    Supports special keys: enter, tab, escape, backspace, delete, space,
    up, down, left, right, home, end, pageup, pagedown, f1-f12.

    Supports modifier combinations: ctrl+a, ctrl+shift+s, alt+f4, etc.

    Args:
        window_title: Full or partial window title (case-insensitive).
        key: Key name or combination (e.g., 'enter', 'ctrl+a', 'f5').
    """
    try:
        return send_key_to_window(window_title, key)
    except RuntimeError as e:
        return str(e)


# --- ADB Tools ---


@mcp.tool()
async def adb_devices() -> str:
    """List all connected Android devices with model, Android version, and screen size.

    Returns a formatted table of connected devices. Use this to find
    device serial numbers for targeting specific devices.
    """
    try:
        devices = list_devices()
    except RuntimeError as e:
        return str(e)

    if not devices:
        return "No ADB devices found."

    lines = [f"Found {len(devices)} device(s):\n"]
    for d in devices:
        lines.append(f"  [{d.serial}] {d.model or 'unknown'}")
        lines.append(f"    State: {d.state}")
        if d.android_version:
            lines.append(f"    Android: {d.android_version}")
        if d.screen_size:
            lines.append(f"    Screen: {d.screen_size}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def adb_device_info(device: str | None = None) -> str:
    """Get detailed information about a connected Android device.

    Args:
        device: Device serial number. If omitted, auto-selects when
            only one device is connected.
    """
    try:
        d = get_device_info(device)
    except RuntimeError as e:
        return str(e)

    lines = [
        f"Device: {d.serial}",
        f"Model: {d.model or 'unknown'}",
        f"Android: {d.android_version or 'unknown'}",
        f"Screen: {d.screen_size or 'unknown'}",
    ]
    return "\n".join(lines)


@mcp.tool()
async def adb_screenshot(device: str | None = None) -> list:
    """Capture the Android device screen as a screenshot.

    Returns the screen image for visual inspection. Use this to see
    what's currently displayed on the device.

    Args:
        device: Device serial number. If omitted, auto-selects when
            only one device is connected.
    """
    try:
        png_data, width, height = capture_device_screen(device)
    except RuntimeError as e:
        return str(e)

    return [
        Image(data=png_data, format="png"),
        f"Device screenshot ({width}x{height})",
    ]


@mcp.tool()
async def adb_tap(x: int, y: int, device: str | None = None) -> str:
    """Tap a point on the Android device screen.

    Args:
        x: X coordinate in pixels.
        y: Y coordinate in pixels.
        device: Device serial number. If omitted, auto-selects when
            only one device is connected.
    """
    try:
        return tap(x, y, device=device)
    except RuntimeError as e:
        return str(e)


@mcp.tool()
async def adb_swipe(
    x1: int, y1: int, x2: int, y2: int,
    duration_ms: int = 300,
    device: str | None = None,
) -> str:
    """Swipe on the Android device screen from one point to another.

    Args:
        x1: Start X coordinate.
        y1: Start Y coordinate.
        x2: End X coordinate.
        y2: End Y coordinate.
        duration_ms: Swipe duration in milliseconds (default 300).
        device: Device serial number. If omitted, auto-selects when
            only one device is connected.
    """
    try:
        return swipe(x1, y1, x2, y2, duration_ms=duration_ms, device=device)
    except RuntimeError as e:
        return str(e)


@mcp.tool()
async def adb_type_text(text: str, device: str | None = None) -> str:
    """Type text on the Android device.

    The text is escaped for the adb shell. Spaces and special characters
    are handled automatically. The device must have a text field focused.

    Args:
        text: The text to type.
        device: Device serial number. If omitted, auto-selects when
            only one device is connected.
    """
    try:
        return type_text(text, device=device)
    except RuntimeError as e:
        return str(e)


@mcp.tool()
async def adb_key(key: str, device: str | None = None) -> str:
    """Send a key event to the Android device.

    Accepts short aliases: home, back, enter, menu, power, volume_up,
    volume_down, tab, delete, space, escape, dpad_up, dpad_down,
    dpad_left, dpad_right, dpad_center, app_switch, camera.
    Also accepts full KEYCODE_* names or numeric key codes.

    Args:
        key: Key name, alias, or numeric code.
        device: Device serial number. If omitted, auto-selects when
            only one device is connected.
    """
    try:
        return key_event(key, device=device)
    except RuntimeError as e:
        return str(e)


@mcp.tool()
async def adb_install(apk_path: str, device: str | None = None) -> str:
    """Install an APK file on the Android device.

    Replaces the existing installation if present (-r flag).

    Args:
        apk_path: Absolute path to the .apk file on the local machine.
        device: Device serial number. If omitted, auto-selects when
            only one device is connected.
    """
    try:
        return install_apk(apk_path, device=device)
    except RuntimeError as e:
        return str(e)


@mcp.tool()
async def adb_list_packages(
    filter_text: str = "",
    device: str | None = None,
) -> str:
    """List installed packages on the Android device.

    Args:
        filter_text: Optional text to filter package names (case-insensitive).
        device: Device serial number. If omitted, auto-selects when
            only one device is connected.
    """
    try:
        packages = list_packages(filter_text=filter_text, device=device)
    except RuntimeError as e:
        return str(e)

    if not packages:
        if filter_text:
            return f"No packages matching '{filter_text}'."
        return "No packages found."

    lines = [f"Found {len(packages)} package(s):\n"]
    for pkg in packages:
        lines.append(f"  {pkg}")
    return "\n".join(lines)


@mcp.tool()
async def adb_launch_app(
    package: str,
    activity: str | None = None,
    device: str | None = None,
) -> str:
    """Launch an app on the Android device.

    If no activity is specified, launches the default launcher activity.

    Args:
        package: Package name (e.g., 'com.example.myapp').
        activity: Optional activity name (e.g., '.MainActivity').
        device: Device serial number. If omitted, auto-selects when
            only one device is connected.
    """
    try:
        return _adb_launch_app_impl(package, activity=activity, device=device)
    except RuntimeError as e:
        return str(e)


@mcp.tool()
async def adb_stop_app(package: str, device: str | None = None) -> str:
    """Force-stop an app on the Android device.

    Args:
        package: Package name (e.g., 'com.example.myapp').
        device: Device serial number. If omitted, auto-selects when
            only one device is connected.
    """
    try:
        return stop_app(package, device=device)
    except RuntimeError as e:
        return str(e)


@mcp.tool()
async def adb_push(
    local_path: str,
    remote_path: str,
    device: str | None = None,
) -> str:
    """Push a file from the local machine to the Android device.

    Args:
        local_path: Path to the file on the local machine.
        remote_path: Destination path on the device (e.g., '/sdcard/file.txt').
        device: Device serial number. If omitted, auto-selects when
            only one device is connected.
    """
    try:
        return push_file(local_path, remote_path, device=device)
    except RuntimeError as e:
        return str(e)


@mcp.tool()
async def adb_pull(
    remote_path: str,
    local_path: str,
    device: str | None = None,
) -> str:
    """Pull a file from the Android device to the local machine.

    Args:
        remote_path: Path on the device (e.g., '/sdcard/file.txt').
        local_path: Destination path on the local machine.
        device: Device serial number. If omitted, auto-selects when
            only one device is connected.
    """
    try:
        return pull_file(remote_path, local_path, device=device)
    except RuntimeError as e:
        return str(e)


def main():
    """Entry point for the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
