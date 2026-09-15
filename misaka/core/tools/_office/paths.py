"""Filesystem fields from FrontierAgent create_file._runtime_ops_paths.

Resolve schema paths, not arbitrary content: a JSON deliverable can itself contain a
literal `path` key. Used by execution and permission checks so secondary outputs agree.
"""
import os
from copy import deepcopy

from misaka.core.tools.path_utils import resolve_to_cwd


def pdf_path(path, args):
    return str(args.get("out") or args.get("output") or args.get("path")
               or os.path.splitext(str(path))[0] + ".pdf")


def resolve_ops(ops, cwd):
    resolved = deepcopy(ops)

    def resolve_fields(args, keys):
        for key in keys:
            value = args.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Office path field {key!r} must be a string")
            if value:
                args[key] = resolve_to_cwd(value, cwd)

    for item in resolved:
        op, args = next(iter(item.items()))
        if op == "export_pdf":
            keys = ("out", "output", "path")
        elif op == "add_image":
            keys = ("image_path", "path")
        else:
            keys = ()
        resolve_fields(args, keys)
        if op == "create":
            for block in args.get("blocks") or []:
                if isinstance(block, dict) and block.get("type") == "image":
                    resolve_fields(block, ("path", "image_path"))
    return resolved


def output_paths(path, ops):
    return list(dict.fromkeys([str(path), *[
        pdf_path(path, item["export_pdf"]) for item in ops if "export_pdf" in item]]))


def input_paths(ops):
    paths = []
    for item in ops:
        op, args = next(iter(item.items()))
        if op == "add_image":
            paths.append(args.get("image_path") or args.get("path"))
        if op == "create":
            paths.extend(block.get("path") or block.get("image_path")
                         for block in args.get("blocks") or []
                         if isinstance(block, dict) and block.get("type") == "image")
    return [path for path in paths if isinstance(path, str) and path]
