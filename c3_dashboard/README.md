# Cerberus C3 boot dashboard

This directory contains the dedicated 1424×280 status display for Cerberus C3.
It is independent of the larger two-node operator dashboard in `dashboard/`.
The HTTP collector updates every five seconds, sampling local C3 each cycle and
remote C1/C2 on an independent 30-second cadence; the local
screen shows separate current and rolling C1/C2/C3 CPU, GPU, and unified-RAM
utilization traces with companion thermal plots. CPU temperature is the
hottest named GB10 cluster sensor and GPU temperature comes from NVIDIA SMI.
The RAM card's companion is explicitly labelled `SOC TEMP`; it is not presented
as RAM temperature. The footer notes that current hardware exposes no dedicated
LPDDR5X sensor, while the API retains a truthful null RAM-temperature field.
The token panel is deliberately different: vLLM exposes one
cluster-wide output counter at the C1 API, so that panel is labelled as a
C1+C2 API aggregate and never invents per-node token attribution.
The API retains only the 60 samples the screen can display, keeping the rolling
window to five minutes without shipping an unused hour of history every poll.
A five-segment background ribbon follows the local Cerberus voice request from
left to right through heard-name detection, ASR, OpenClaw, TTS, and playback.
This keeps the four telemetry cards available while making the active stage and
completed stages visible across the whole display.

The fixed 1424x280 view includes a dependency-free, 178x35 pixel canvas behind
the telemetry. Six inexpensive software-rendered scenes rotate every 30
seconds at one frame per second, and the foreground drifts by one pixel at
each scene change. This varies otherwise static pixels without moving the
current values far enough to hurt glanceability. Reduced-motion clients render
only one frame per 30-second scene. No image, font, shader, or other asset is
downloaded.

The ambient art is separate from the TFT-maintenance sweep. After five quiet
minutes a topmost, exact-black 48-pixel band crosses the 1424x280 panel in 3.2
seconds, then repeats no more than once per 30 minutes. At that speed every row
stays black for about 0.47 seconds—over nine times the DP-0101's specified 50
ms response time. A recognized voice command, model/TTS work, changed health,
recovery, or local input postpones a quiet-time pass; ignored background speech
that merely invokes ASR does not. An independent 30-minute panel-care deadline
still forces a complete pass during continuous work, and activity cannot cancel
that forced pass. The dashboard remains visible outside the band.
DeskPi publishes no image-retention recovery duration or cadence for this
panel. The response-time figure is used only to ensure the band is not moving
faster than the liquid crystals can visibly transition; the 30-minute cadence
is a deliberately low-disruption heuristic, not a vendor pixel-refresh cycle.

The on-screen deployment has two services:

```text
dgx-spark-c3-dashboard.service  -> loopback Python collector on :9763
                                      |
dgx-spark-c3-kiosk.service      -> rootless Xorg on VT 7
                                      -> GTK4/WebKitGTK view
                                      -> http://127.0.0.1:9763/
                                      -> validated logind-session cleanup
```

There is no display manager, desktop session, or window manager in this path.
The kiosk runs as the selected unprivileged account. `PAMName=login` lets
systemd-logind grant that active VT session the DRM and input device handles
needed by rootless Xorg. A privileged `chvt 7` pre-start step makes that VT
active before Xorg requests the DRM lease; without it, logind returns a paused
DRM descriptor on a headless boot whose foreground console is still VT 1.
Because pam_systemd moves the resulting process tree into a login scope, the
installer also places a small cleanup helper at
`/usr/local/libexec/dgx-spark-c3-terminate-kiosk-session`. The installed copy
is root-owned. At stop and post-stop it reads a private numeric session ID and
requires the configured UID, account name, local `login` service, non-remote
state, and `tty7` before stopping that exact logind scope. This
prevents Xorg or WebKit processes from surviving a service restart.

## Verified C3 runtime

A read-only audit on Cerberus C3 found Ubuntu 24.04, systemd 255, Xorg 21.1.12,
GTK 4.14.5, and WebKitGTK 2.52.3. The required `startx`, `xinit`, `xauth`,
`mcookie`, `xrandr`, `xset`, `xsetroot`, `dbus-run-session`, Python GI, GTK4, and WebKit
6.0 components are already installed. No package installation is part of this
deployment.

The connected panel identified itself to the NVIDIA driver as `YTH HS156PC`
on `DFP-0`; its EDID native mode is 1424×280. XRandR's public output name is
selected at session startup, so the service does not depend on the driver's
internal connector name.

C3's default target is already `multi-user.target`, and GDM was static and
inactive at audit time. The installer deliberately leaves both settings
unchanged.

## Verify without changing the host

Run from a checkout that already contains `server.py` and the static assets:

```bash
c3_dashboard/scripts/install.sh verify
c3_dashboard/tests/run.sh
```

`verify` renders both units into a temporary directory, checks the environment
contract, compiles the Python sources into that temporary directory, imports
the installed GTK/WebKit runtime, validates the shell launchers, and runs
`systemd-analyze verify`. It does not use sudo or touch systemd.
The test runner executes every Python test it discovers. It also runs the UI
tests when Node is present and explicitly reports the UI skip on C3, where Node
is not required at runtime.

## Install on C3

Sync this checkout to Cerberus C3, then run there as the intended runtime user:

```bash
c3_dashboard/scripts/install.sh install
c3_dashboard/scripts/install.sh enable
```

`install` only places the environment and units. `enable` additionally enables
both units in `multi-user.target` for future boots; it does not start them and
does not alter the default target or GDM.

The installer rejects UID 0 as the service account. If invoked from a direct
root shell, name the intended unprivileged account explicitly, for example
`SPARK_SERVICE_USER=catid c3_dashboard/scripts/install.sh install`.

Before the first live start, make sure no old X test or display manager owns the
GPU/VT. The `start` action refuses to take over from either one:

```bash
systemctl is-active display-manager.service
pgrep -a Xorg || true
sudo systemctl stop display-manager.service  # only if the first check is active
c3_dashboard/scripts/install.sh start
```

An existing `/etc/default/dgx-spark-c3-dashboard` is preserved only after it
passes the same strict loopback, interval, URL, SSH-path, display, and range
validation as a new configuration. An invalid live file aborts before units
are installed or enabled. Use `--replace-environment` only for a deliberate
replacement:

```bash
c3_dashboard/scripts/install.sh start --replace-environment
```

The environment is installed root-owned with mode 0600. Its defaults pin the
five-second interval, the three historical `cerberus1`-`cerberus3` DNS aliases,
C1's vLLM metrics URL, the local 9763 port, the existing cluster SSH key, C3's
existing `known_hosts`, the voice heartbeat under `/run`, and the native panel
mode. Host-key entries still need to be verified through a trusted channel
before unattended use.

Remote C1/C2 samples use OpenSSH connection sharing and, more importantly, one
remote command per node every 30 seconds rather than a new PAM session every
five seconds. Cached samples retain their original timestamp, expose
`sample_cached=true`, accumulate an honest `age_seconds`, and become `stale`
after 65 seconds instead of masquerading as fresh. `systemd` owns a mode-0700
`/run/dgx-spark-c3-dashboard` directory; `ControlMaster=auto`, OpenSSH's `%C`
destination hash, strict host-key checking, and a 40-second `ControlPersist`
bound reuse to the same destination tuple and a short time window. The master
processes stay in the service cgroup and its runtime directory is removed on
stop, so a service restart cannot inherit stale sockets. The metrics scrape
uses curl's total `--max-time` plus an independent process-group watchdog; DNS,
headers, and a body that trickles forever all share the same scrape deadline.

## Boot and failure behavior

The collector binds its loopback listener before starting the first scrape and
uses `Type=notify`; the kiosk's required dependency is released only after that
listener can serve a valid `starting` snapshot. The collector has unlimited
systemd restart attempts, including after an unexpected clean exit. The kiosk likewise has
unlimited restart attempts with a 15-second delay. On every start
it launches rootless Xorg with TCP listening disabled, discovers a connected
XRandR output, disables other active outputs, and requires the selected output
to accept 1424×280 before WebKit starts. X display `:0` and VT 7 are fixed so
the Xorg VT always matches systemd's controlling TTY and PAM/logind session.
Xorg starts with a black root window, the session reapplies an exact black root
color after setting the native mode, and WebKit's own underlay is exact black.
Consequently startup, navigation, and renderer-recovery gaps do not expose a
bright frame around the page or a maintenance sweep.

`startx` creates a fresh MIT-MAGIC-COOKIE, keeps the client authority in the
mode-0700 runtime home and the server authority in the service's private temp
directory, and passes an explicit `-auth` file to Xorg. Together with
`-nolisten tcp`, this protects the remaining local UNIX display socket from
other local users. The kiosk's `XDG_RUNTIME_DIR`, transient D-Bus socket,
client xauth file, caches, and state use writable service-private runtime or
temp storage; none relies on the read-only operator home.

If the panel is unplugged, the X session exits after a short bounded probe and
systemd tries again later. This does not hold up `multi-user.target`, start a
desktop, or make the HTTP service unavailable. Hot-plugging the panel is enough
for a later attempt to recover. If `/dev/tty0` or no numbered DRM card exists,
systemd skips the kiosk through unit conditions while leaving the collector
usable.

The WebKit client is restricted to the configured loopback origin, denies web
permissions, hides the pointer, disables screen blanking when supported, and
retries initial page-load failures every five seconds. A failed navigation is
handled instead of showing WebKit's stock error page: the kiosk displays a
self-contained exact-black fallback until retry succeeds. Its home, caches, and
transient D-Bus session live under a systemd-owned runtime directory rather
than the operator's home. WebKit's nested bubblewrap/FUSE sandbox is disabled
because it cannot mount inside the already hardened systemd namespace; the
unit retains its systemd restrictions and the kiosk cannot navigate away from
the exact loopback dashboard origin. GTK and WebKit use software rendering for
this small page: on bare GB10 Xorg, accelerated WebKit surfaces can remain
white without a compositor even while the page is loaded and polling.
X11 blanking and DPMS intentionally remain disabled. The in-page maintenance
sweep can run while local voice and cluster polling continues; a DPMS-off HDMI
panel would require a second privileged wake controller and may show a
vendor-specific no-signal screen.

## Operations

Check the two processes and the local API:

```bash
systemctl status dgx-spark-c3-dashboard.service dgx-spark-c3-kiosk.service
journalctl -u dgx-spark-c3-dashboard.service -u dgx-spark-c3-kiosk.service \
  -n 100 --no-pager
curl -fsS http://127.0.0.1:9763/api/status | jq .
curl -fsS http://127.0.0.1:9763/api/voice-status | jq .
```

During a quiet period, verify the sweep at the final X framebuffer rather than
trusting its CSS or DOM state. The verifier watches a small top-row probe until
the band arrives, then succeeds only after every one of the 280 complete rows
has remained exact RGB black for at least three samples spanning 100 ms:

```bash
sudo -u catid c3_dashboard/scripts/verify-black-sweep.py \
  --display :0 \
  --xauthority /run/dgx-spark-c3-kiosk/.Xauthority \
  --wait-seconds 1860
```

The 1,860-second default covers the independent 30-minute maximum deferral plus
a one-minute scheduling margin, so the verifier can observe a healthy sweep
even during continuous work. The check samples the real X root, not the DOM. It verifies digital scanout
content; an LCD still has uniform backlight glow while the requested row is
black. This TFT has no OLED-style pixel-refresh cycle: the sweep and existing
nine-position one-pixel nudge simply avoid leaving identical liquid-crystal
states and fixed chart edges in place indefinitely.

`systemctl status` can show zero kiosk tasks and only the small PAM handler's
memory even while the screen is active: logind accounts Xorg and WebKit in the
corresponding `session-*.scope`. Use `loginctl list-sessions` followed by
`loginctl session-status ID` when auditing the full display process tree. The
service's stop hooks still terminate that validated scope.

The voice bridge atomically updates
`/run/cerberus3-voice-bridge/status.json` every two seconds and at every stage
transition. The dashboard reads it without following symlinks, caps it at 32
KiB, accepts only schema version 1 for `cerberus-voice`, and exposes only a
fixed operational-field allowlist. Transcripts, requests, replies, API tokens,
and raw exception messages are neither read into nor returned by the dashboard.
Three missed two-second heartbeats mark the voice state stale after six seconds.
If a future schema-1 producer adds `dependencies` or `readiness.dependencies`
for ASR, OpenClaw, and TTS, the reader exposes only those fixed names and their
readiness. Explicitly unready dependencies degrade voice health; an absent or
unknown extension remains neutral for today's producer.
The background voice progress ribbon polls its dedicated endpoint every 750 ms, independently of
the five-second SSH/metrics collector. A voice heartbeat or endpoint failure
therefore cannot mark the cluster telemetry offline.

The kiosk logs the selected XRandR output and native mode. If more than one
display is attached, set `C3_KIOSK_OUTPUT` in the live environment to the exact
desired XRandR output name and restart only the kiosk; every other connected
output is turned off atomically while the framebuffer is set to 1424×280.

To roll back without touching GDM or the boot target:

```bash
sudo systemctl disable --now \
  dgx-spark-c3-kiosk.service dgx-spark-c3-dashboard.service
```

The installed files remain available for a later re-enable; rollback does not
delete the checkout, environment, SSH material, or collected source data.
