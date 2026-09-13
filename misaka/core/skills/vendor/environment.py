# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / hermes_constants.py; see PROVENANCE.json and LICENSE.
import os

def _read_proc(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _proc_file_has_marker(path: str, markers: tuple[str, ...]) -> bool:
    content = _read_proc(path)
    return any(marker in content for marker in markers)


def _detect_container() -> bool:
    if (
        os.path.exists("/.dockerenv")
        or os.path.exists("/run/.containerenv")
        or os.environ.get("KUBERNETES_SERVICE_HOST")
        or _proc_file_has_marker("/proc/1/cgroup", ("docker", "podman", "/lxc/", "kubepods", "containerd", "crio"))
    ):
        return True
    # cgroup v2: /proc/1/cgroup is just "0::/"; the runtime still shows in mountinfo — but ONLY on
    # the root ("/") mount line. A host that merely *runs* containers exposes every container's
    # overlay lowerdir (``lowerdir=/var/lib/containerd/...``) at non-root mount points, which a
    # whole-file scan misread as "inside a container" and flipped subprocess HOME (#58135).
    return _root_mount_has_marker("/proc/self/mountinfo", ("kubepods", "containerd", "crio"))


def _root_mount_has_marker(path: str, markers: tuple[str, ...]) -> bool:
    """mountinfo field 5 (index 4) is the mount point; only the root ("/") line is the process's own rootfs."""
    root_lines = [line for line in _read_proc(path).splitlines() if len(f := line.split()) >= 5 and f[4] == "/"]
    return any(marker in line for line in root_lines for marker in markers)

