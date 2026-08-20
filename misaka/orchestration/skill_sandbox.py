"""技能只读强制（宪法④ / D2）：Sister 拿到的是只读副本，物理上够不到真技能树。

ponytail: 不用挂载/chflags（会污染用户自己的文件），复制+去写权最省且可验证。
单件技能都是 KB-MB 级；超 SIZE_CAP 直接报错拒跑——fail-closed，不留后门。
"""
import os
import shutil
import stat

SIZE_CAP_MB = int(os.environ.get("MISAKA_SKILL_COPY_CAP_MB", "200"))


def _tree_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _strip_write(path):
    for root, dirs, files in os.walk(path):
        for name in files + dirs:
            p = os.path.join(root, name)
            try:
                os.chmod(p, os.stat(p).st_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
            except OSError:
                pass


def readonly_copies(skill_dirs, dest_root):
    """把技能目录复制成只读副本，返回副本路径列表。空输入即空返回。"""
    if not skill_dirs:
        return []
    total = sum(_tree_size(d) for d in skill_dirs)
    if total > SIZE_CAP_MB * 1024 * 1024:
        raise RuntimeError(
            f"skills.list 合计 {total / 1e6:.0f}MB 超上限 {SIZE_CAP_MB}MB——"
            "收窄 skills.list 或调高 MISAKA_SKILL_COPY_CAP_MB（不许绕过只读）")
    os.makedirs(dest_root, exist_ok=True)
    copies = []
    for d in skill_dirs:
        dst = os.path.join(dest_root, os.path.basename(d.rstrip("/")))
        if os.path.exists(dst):
            cleanup(dst)   # 上一轮的只读副本裸 rmtree 删不掉（只读子目录拒 unlink），
            #              残骸会让 copytree 抛 FileExistsError——先加写权再删
        shutil.copytree(d, dst, symlinks=False, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        _strip_write(dst)
        copies.append(dst)
    return copies


def cleanup(dest_root):
    """删只读副本（要先加回写权，否则删不掉）。"""
    if not os.path.isdir(dest_root):
        return
    for root, dirs, files in os.walk(dest_root):
        for name in files + dirs:
            p = os.path.join(root, name)
            try:
                os.chmod(p, os.stat(p).st_mode | stat.S_IWUSR)
            except OSError:
                pass
    shutil.rmtree(dest_root, ignore_errors=True)


if __name__ == "__main__":
    import tempfile
    src = tempfile.mkdtemp()
    os.makedirs(os.path.join(src, "demo-skill"))
    real = os.path.join(src, "demo-skill", "SKILL.md")
    with open(real, "w", encoding="utf-8") as f:
        f.write("原始内容")
    dest = os.path.join(tempfile.mkdtemp(), "ro")
    copies = readonly_copies([os.path.join(src, "demo-skill")], dest)
    target = os.path.join(copies[0], "SKILL.md")
    assert open(target, encoding="utf-8").read() == "原始内容"
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write("篡改")
        raise AssertionError("副本竟然可写——只读强制失效！")
    except PermissionError:
        pass
    assert open(real, encoding="utf-8").read() == "原始内容", "真树被动了"
    cleanup(dest)
    assert not os.path.exists(dest)
    print("readonly_skills selfcheck ok — 副本写入被拒(PermissionError)，真技能树未受影响")
