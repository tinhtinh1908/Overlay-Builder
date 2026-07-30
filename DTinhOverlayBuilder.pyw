import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
import xml.etree.ElementTree as ET


APP_TITLE = "DTinh Overlay Builder"
APP_VERSION = "3.0.1"

BG = "#0b0e14"
CARD = "#151922"
CARD_ALT = "#1b202b"
TEXT = "#f4f7fb"
SUB = "#9ba7b8"
BLUE = "#4c8dff"
BLUE_DARK = "#263d65"
BORDER = "#2a3240"
SUCCESS = "#31c48d"
ERROR = "#ff6464"

PACKAGE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+$")
RESOURCE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-$]+$")
FORMAT_SPECIFIER_RE = re.compile(
    r"%(?!%|n)(?:(\d+)\$)?[-#+ 0,(<]*\d*(?:\.\d+)?[tT]?[a-zA-Z]"
)
STRING_OPEN_TAG_RE = re.compile(r"<string\b[^>]*>")
NAME_ATTRIBUTE_RE = re.compile(r"""\bname\s*=\s*(["'])(.*?)\1""")
FORMATTED_ATTRIBUTE_RE = re.compile(r"""(\bformatted\s*=\s*)(["'])(.*?)\2""")
RESOURCE_REFERENCE_RE = re.compile(
    r"^@(?P<private>\*)?(?:(?P<package>[A-Za-z_][A-Za-z0-9_.]*):)?"
    r"(?P<type>[A-Za-z_][A-Za-z0-9_]*)/(?P<name>[A-Za-z0-9_.\-$]+)$"
)
RESOURCE_TAG_TO_TYPE = {
    "string": "string",
    "plurals": "plurals",
    "string-array": "array",
    "array": "array",
    "integer-array": "array",
}


def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE = app_dir()
TOOLS = BASE / "tools"
OUT = BASE / "output"
WORK = Path(tempfile.gettempdir()) / "DTinhOverlayBuilder"
WORK.mkdir(parents=True, exist_ok=True)
OUT.mkdir(exist_ok=True)


@dataclass(frozen=True)
class BuildConfig:
    target_apk: Path
    translation_xmls: tuple
    system_apks: tuple
    keystore: Path
    alias: str
    keystore_password: str
    key_password: str
    output_name: str
    overlay_package: str
    priority: int
    add_base_and_chinese_values: bool


@dataclass(frozen=True)
class ResourceEntry:
    source_tag: str
    resource_type: str
    name: str


def sanitize_output_name(value):
    value = re.sub(r"\.apk$", "", value.strip().lower())
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-._")
    return value or "overlay"


def automatic_overlay_package(target_package, output_name):
    suffix = f"{target_package}.{sanitize_output_name(output_name)}".lower()
    suffix = re.sub(r"[^a-z0-9_.]", "_", suffix)
    suffix = ".".join(part if part and not part[0].isdigit() else f"p_{part}" for part in suffix.split("."))
    return f"com.dtinh.overlay.{suffix}"


def parse_resource_entries(path):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise RuntimeError(f"XML không hợp lệ: {exc}") from exc

    if root.tag != "resources":
        raise RuntimeError("Nút gốc của XML phải là <resources>")

    entries = []
    seen = set()
    duplicates = []
    invalid = []
    for node in root:
        source_tag = node.tag
        resource_type = RESOURCE_TAG_TO_TYPE.get(source_tag)
        if source_tag == "item" and node.get("type") == "string":
            resource_type = "string"
        if not resource_type:
            continue
        name = node.get("name")
        if not name:
            invalid.append(f"<{source_tag}> thiếu name")
            continue
        if not RESOURCE_NAME_RE.fullmatch(name):
            invalid.append(name)
            continue
        identity = (resource_type, name)
        if identity in seen:
            duplicates.append(f"{resource_type}/{name}")
            continue
        seen.add(identity)
        entries.append(ResourceEntry(source_tag, resource_type, name))

    if invalid:
        sample = ", ".join(invalid[:8])
        raise RuntimeError(f"Có resource sai định dạng: {sample}")
    if duplicates:
        sample = ", ".join(duplicates[:8])
        raise RuntimeError(f"XML có resource trùng type/name: {sample}")
    if not entries:
        supported = ", ".join(f"<{tag}>" for tag in RESOURCE_TAG_TO_TYPE)
        raise RuntimeError(f"Không tìm thấy resource hỗ trợ: {supported}")
    return entries


def parse_resource_files(paths):
    all_entries = []
    origins = {}
    duplicates = []
    for path in paths:
        path = Path(path)
        for entry in parse_resource_entries(path):
            identity = (entry.resource_type, entry.name)
            if identity in origins:
                duplicates.append(
                    f"{entry.resource_type}/{entry.name} "
                    f"({origins[identity].name} và {path.name})"
                )
                continue
            origins[identity] = path
            all_entries.append(entry)
    if duplicates:
        sample = ", ".join(duplicates[:8])
        raise RuntimeError(f"Các file XML có resource trùng type/name: {sample}")
    return all_entries


def needs_formatted_false(text):
    substitutions = list(FORMAT_SPECIFIER_RE.finditer(text))
    return len(substitutions) > 1 and any(match.group(1) is None for match in substitutions)


def prepare_translation_xml(source, destination, target_package=None, local_resources=()):
    text = Path(source).read_text(encoding="utf-8-sig")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise RuntimeError(f"XML không hợp lệ: {exc}") from exc

    fixes = set()
    reference_rewrites = {}
    local_resources = set(local_resources)
    for node in root:
        if node.tag != "string":
            for child in node.iter():
                value = (child.text or "").strip()
                reference = RESOURCE_REFERENCE_RE.fullmatch(value)
                if not reference or reference.group("package") or not target_package:
                    continue
                identity = (reference.group("type"), reference.group("name"))
                if identity in local_resources or reference.group("type") == "id":
                    continue
                qualified = (
                    f"@*{target_package}:{reference.group('type')}/{reference.group('name')}"
                )
                reference_rewrites[value] = qualified
            continue
        name = node.get("name")
        value = "".join(node.itertext())
        if name and needs_formatted_false(value) and node.get("formatted") != "false":
            fixes.add(name)

    def patch_open_tag(match):
        tag = match.group(0)
        name_match = NAME_ATTRIBUTE_RE.search(tag)
        if not name_match or name_match.group(2) not in fixes:
            return tag
        if FORMATTED_ATTRIBUTE_RE.search(tag):
            return FORMATTED_ATTRIBUTE_RE.sub(
                lambda attr: f'{attr.group(1)}{attr.group(2)}false{attr.group(2)}',
                tag,
                count=1,
            )
        return tag[:-1] + ' formatted="false">'

    normalized = STRING_OPEN_TAG_RE.sub(patch_open_tag, text)
    for original, qualified in sorted(reference_rewrites.items(), key=lambda item: -len(item[0])):
        normalized = re.sub(
            rf"(?<=>)(\s*){re.escape(original)}(\s*)(?=<)",
            lambda match: f"{match.group(1)}{qualified}{match.group(2)}",
            normalized,
        )
    destination.write_text(normalized, encoding="utf-8")
    return tuple(sorted(fixes)), tuple(sorted(reference_rewrites))


def make_link_command(aapt2, output_apk, manifest, android_jar, system_apks, target_apk, compiled):
    command = [
        str(aapt2),
        "link",
        "-o",
        str(output_apk),
        "--manifest",
        str(manifest),
        "-I",
        str(android_jar),
    ]
    for dependency in system_apks:
        command.extend(["-I", str(dependency)])
    command.extend([
        "-I",
        str(target_apk),
        "--auto-add-overlay",
        "--no-resource-removal",
        str(compiled),
    ])
    return command


def write_manifest(path, overlay_package, target_package, priority):
    package_attr = html.escape(overlay_package, quote=True)
    target_attr = html.escape(target_package, quote=True)
    manifest = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"\n'
        f'    package="{package_attr}"\n'
        '    android:versionCode="1"\n'
        f'    android:versionName="{APP_VERSION}">\n'
        '    <application\n'
        '        android:hasCode="false"\n'
        '        android:label="DTinh Vietnamese Overlay" />\n'
        '    <overlay\n'
        f'        android:targetPackage="{target_attr}"\n'
        '        android:isStatic="true"\n'
        f'        android:priority="{priority}" />\n'
        '</manifest>\n'
    )
    path.write_text(manifest, encoding="utf-8")


def stage_overlay_project(
    temp_path,
    translation_xmls,
    entries,
    overlay_package,
    target_package,
    priority,
    add_base_and_chinese_values=False,
):
    resource_directories = ["values-vi"]
    if add_base_and_chinese_values:
        resource_directories.extend(("values", "values-zh-rCN"))

    staged_files = []
    format_fixes = []
    reference_fixes = []
    local_resources = {(entry.resource_type, entry.name) for entry in entries}
    for index, translation_xml in enumerate(translation_xmls, start=1):
        staged_file = temp_path / "res" / "values-vi" / f"resources_{index:03d}.xml"
        staged_file.parent.mkdir(parents=True, exist_ok=True)
        fixed_keys, fixed_references = prepare_translation_xml(
            translation_xml,
            staged_file,
            target_package=target_package,
            local_resources=local_resources,
        )
        format_fixes.extend((Path(translation_xml).name, key) for key in fixed_keys)
        reference_fixes.extend(
            (Path(translation_xml).name, reference) for reference in fixed_references
        )
        staged_files.append(staged_file)
        for directory_name in resource_directories[1:]:
            destination = temp_path / "res" / directory_name / staged_file.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged_file, destination)

    write_manifest(temp_path / "AndroidManifest.xml", overlay_package, target_package, priority)
    return (
        tuple(staged_files),
        tuple(resource_directories),
        tuple(format_fixes),
        tuple(reference_fixes),
    )


class App:
    def __init__(self, root):
        self.root = root
        self.target_apk = tk.StringVar()
        self.translation_xmls = []
        self.keystore = tk.StringVar()
        self.alias = tk.StringVar()
        self.keystore_password = tk.StringVar()
        self.key_password = tk.StringVar()
        self.output_name = tk.StringVar()
        self.overlay_package = tk.StringVar()
        self.priority = tk.StringVar(value="999")
        self.add_base_and_chinese_values = tk.BooleanVar(value=False)
        self.system_apks = []
        self.is_building = False

        root.title(f"{APP_TITLE} {APP_VERSION}")
        root.geometry("1040x680")
        root.minsize(920, 620)
        root.configure(bg=BG)
        self.build_ui()

    def label(self, parent, text, size=10, bold=False, color=TEXT, **kwargs):
        return tk.Label(
            parent,
            text=text,
            bg=parent["bg"],
            fg=color,
            font=("Segoe UI", size, "bold" if bold else "normal"),
            **kwargs,
        )

    def button(self, parent, text, command, primary=False, width=None):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=BLUE if primary else CARD_ALT,
            fg="white" if primary else TEXT,
            activebackground="#3b7de8" if primary else "#252c39",
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=12,
            pady=6,
            width=width,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )

    def entry(self, parent, variable, show=None):
        widget = tk.Entry(
            parent,
            textvariable=variable,
            show=show,
            relief="flat",
            bd=0,
            bg="#202631",
            fg=TEXT,
            insertbackground=TEXT,
            font=("Segoe UI", 10),
            selectbackground=BLUE,
            selectforeground="white",
        )
        widget.configure(highlightthickness=1, highlightbackground=BORDER, highlightcolor=BLUE)
        return widget

    def section(self, parent, title):
        frame = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        frame.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(frame, bg=CARD)
        inner.pack(fill="both", expand=True, padx=14, pady=12)
        self.label(inner, title, 11, True).pack(anchor="w", pady=(0, 8))
        return inner

    def file_row(self, parent, title, variable, filetypes, command=None):
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=(0, 9))
        self.label(row, title, 8, True, SUB).pack(anchor="w", pady=(0, 4))
        line = tk.Frame(row, bg=CARD)
        line.pack(fill="x")
        self.entry(line, variable).pack(side="left", fill="x", expand=True, ipady=6)
        pick_command = command or (lambda: self.pick_file(variable, filetypes))
        self.button(line, "Chọn…", pick_command).pack(side="left", padx=(8, 0))

    def listbox(self, parent, height):
        frame = tk.Frame(parent, bg=CARD)
        frame.pack(fill="x")
        widget = tk.Listbox(
            frame,
            height=height,
            selectmode="extended",
            bg="#101722",
            fg=TEXT,
            selectbackground=BLUE_DARK,
            selectforeground="white",
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=BLUE,
            font=("Cascadia Mono", 9),
        )
        scrollbar = tk.Scrollbar(frame, orient="vertical", command=widget.yview)
        widget.configure(yscrollcommand=scrollbar.set)
        widget.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return widget

    def field_grid(self, parent, fields):
        grid = tk.Frame(parent, bg=CARD)
        grid.pack(fill="x")
        for index, (title, variable, show) in enumerate(fields):
            cell = tk.Frame(grid, bg=CARD)
            column = index % 2
            cell.grid(
                row=index // 2,
                column=column,
                sticky="ew",
                padx=(0, 6) if column == 0 else (6, 0),
                pady=(0, 8),
            )
            self.label(cell, title, 8, True, SUB).pack(anchor="w", pady=(0, 4))
            self.entry(cell, variable, show).pack(fill="x", ipady=6)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

    def build_ui(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=18, pady=(12, 10))
        title_line = tk.Frame(header, bg=BG)
        title_line.pack(fill="x")
        self.label(title_line, APP_TITLE, 18, True).pack(side="left")
        self.label(title_line, f"v{APP_VERSION} RELEASE", 9, True, BLUE).pack(
            side="left", padx=(9, 0), pady=(8, 0)
        )
        self.label(
            header,
            "Tạo APK RRO tiếng Việt cho Android và HyperOS",
            9,
            False,
            SUB,
        ).pack(anchor="w", pady=(3, 0))

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=18)

        columns = tk.Frame(body, bg=BG)
        columns.pack(fill="both", expand=True)
        left_column = tk.Frame(columns, bg=BG)
        right_column = tk.Frame(columns, bg=BG)
        left_column.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        right_column.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        columns.columnconfigure(0, weight=1, uniform="main")
        columns.columnconfigure(1, weight=1, uniform="main")
        columns.rowconfigure(0, weight=1)

        source = self.section(left_column, "Nguồn")
        self.file_row(
            source,
            "APK đích",
            self.target_apk,
            [("Android APK", "*.apk"), ("Tất cả", "*.*")],
            command=self.pick_target_apk,
        )
        resource_header = tk.Frame(source, bg=CARD)
        resource_header.pack(fill="x", pady=(0, 4))
        self.label(resource_header, "XML tiếng Việt", 8, True, SUB).pack(side="left")
        self.translation_count = self.label(resource_header, "0 file", 8, False, SUB)
        self.translation_count.pack(side="right")
        self.translation_list = self.listbox(source, 4)
        resource_actions = tk.Frame(source, bg=CARD)
        resource_actions.pack(fill="x", pady=(7, 0))
        self.button(resource_actions, "Thêm XML", self.add_translation_xmls).pack(side="left")
        self.button(resource_actions, "Xóa", self.remove_translation_xmls).pack(
            side="left", padx=(7, 0)
        )

        dependencies = self.section(left_column, "APK phụ thuộc")
        dependency_header = tk.Frame(dependencies, bg=CARD)
        dependency_header.pack(fill="x", pady=(0, 4))
        self.label(dependency_header, "Framework hoặc thư viện hệ thống", 8, True, SUB).pack(
            side="left"
        )
        self.dependency_count = self.label(dependency_header, "0 file", 8, False, SUB)
        self.dependency_count.pack(side="right")
        self.dependency_list = self.listbox(dependencies, 3)
        dependency_actions = tk.Frame(dependencies, bg=CARD)
        dependency_actions.pack(fill="x", pady=(7, 0))
        self.button(dependency_actions, "Thêm APK", self.add_system_apks).pack(side="left")
        self.button(dependency_actions, "Xóa", self.remove_system_apks).pack(
            side="left", padx=(7, 0)
        )
        self.button(dependency_actions, "↑", lambda: self.move_dependency(-1), width=2).pack(
            side="right"
        )
        self.button(dependency_actions, "↓", lambda: self.move_dependency(1), width=2).pack(
            side="right", padx=(0, 6)
        )

        config = self.section(right_column, "Ký và đầu ra")
        self.file_row(
            config,
            "Keystore",
            self.keystore,
            [("Keystore", "*.jks *.keystore"), ("Tất cả", "*.*")],
        )
        self.field_grid(
            config,
            [
                ("Alias", self.alias, None),
                ("Tên APK", self.output_name, None),
                ("Mật khẩu keystore", self.keystore_password, "*"),
                ("Mật khẩu key (có thể để trống)", self.key_password, "*"),
                ("Overlay package (tự động nếu trống)", self.overlay_package, None),
                ("Priority", self.priority, None),
            ],
        )

        options = tk.Frame(config, bg=CARD)
        options.pack(fill="x", pady=(2, 0))
        tk.Checkbutton(
            options,
            text="Xuất thêm values và values-zh-rCN",
            variable=self.add_base_and_chinese_values,
            bg=CARD,
            fg=TEXT,
            activebackground=CARD,
            activeforeground=TEXT,
            selectcolor="#202631",
            font=("Segoe UI", 9),
        ).pack(side="left")
        self.output_hint = self.label(options, "dtinh-overlay.apk", 9, True, BLUE)
        self.output_hint.pack(side="right")
        self.output_name.trace_add("write", self.update_output_hint)

        action = tk.Frame(right_column, bg=BG)
        action.pack(fill="x", pady=(0, 7))
        self.build_button = self.button(action, "TẠO OVERLAY", self.start_build, primary=True)
        self.build_button.pack(side="left")
        self.button(action, "Mở output", self.open_output).pack(side="left", padx=(8, 0))
        self.status = self.label(action, "Sẵn sàng", 9, False, SUB, anchor="e")
        self.status.pack(side="right", fill="x", expand=True)

        self.progress = tk.Canvas(right_column, height=4, bg="#252c38", highlightthickness=0)
        self.progress.pack(fill="x", pady=(0, 7))
        self.progress_bar = self.progress.create_rectangle(0, 0, 0, 4, fill=BLUE, outline=BLUE)

        log_card = tk.Frame(right_column, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        log_card.pack(fill="both", expand=True, pady=(0, 8))
        log_header = tk.Frame(log_card, bg=CARD)
        log_header.pack(fill="x", padx=10, pady=(7, 3))
        self.label(log_header, "Nhật ký", 9, True, SUB).pack(side="left")
        self.button(log_header, "Xóa", self.clear_log).pack(side="right")
        self.log = tk.Text(
            log_card,
            height=5,
            bg="#0d1420",
            fg="#dbeafe",
            insertbackground="white",
            relief="flat",
            bd=0,
            font=("Cascadia Mono", 9),
            wrap="word",
        )
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.log.tag_configure("success", foreground=SUCCESS)
        self.log.tag_configure("error", foreground=ERROR)
        self.log.tag_configure("info", foreground=BLUE)
        self.log.insert("end", "Sẵn sàng tạo overlay.\n")
        self.log.configure(state="disabled")

    def pick_file(self, variable, filetypes):
        path = filedialog.askopenfilename(filetypes=filetypes)
        if path:
            variable.set(path)

    def pick_target_apk(self):
        path = filedialog.askopenfilename(filetypes=[("Android APK", "*.apk"), ("Tất cả", "*.*")])
        if not path:
            return
        new_target = str(Path(path).resolve())
        target_changed = self.normalized_path(new_target) != self.normalized_path(
            self.target_apk.get()
        )
        self.target_apk.set(new_target)
        if target_changed:
            self.translation_xmls.clear()
            self.refresh_translation_list()
            self.output_name.set("")
            self.overlay_package.set("")
        self.remove_duplicate_target_dependency()

    def add_translation_xmls(self):
        paths = filedialog.askopenfilenames(
            title="Chọn một hoặc nhiều file resource tiếng Việt",
            filetypes=[("XML", "*.xml"), ("Tất cả", "*.*")],
        )
        if not paths:
            return
        existing = {self.normalized_path(path) for path in self.translation_xmls}
        for path in paths:
            normalized = self.normalized_path(path)
            if normalized not in existing:
                self.translation_xmls.append(str(Path(path).resolve()))
                existing.add(normalized)
        self.refresh_translation_list()

    def remove_translation_xmls(self):
        for index in reversed(self.translation_list.curselection()):
            del self.translation_xmls[index]
        self.refresh_translation_list()

    def refresh_translation_list(self):
        self.translation_list.delete(0, "end")
        for index, path in enumerate(self.translation_xmls, start=1):
            self.translation_list.insert("end", f"{index:02d}. {path}")
        self.translation_count.config(text=f"{len(self.translation_xmls)} file")

    def add_system_apks(self):
        paths = filedialog.askopenfilenames(
            title="Chọn APK hệ thống bổ sung",
            filetypes=[("Android APK", "*.apk"), ("APK hoặc JAR", "*.apk *.jar"), ("Tất cả", "*.*")],
        )
        if not paths:
            return
        target = self.normalized_path(self.target_apk.get())
        existing = {self.normalized_path(path) for path in self.system_apks}
        skipped_target = False
        for path in paths:
            normalized = self.normalized_path(path)
            if normalized == target:
                skipped_target = True
                continue
            if normalized not in existing:
                self.system_apks.append(str(Path(path).resolve()))
                existing.add(normalized)
        self.refresh_dependency_list()
        if skipped_target:
            messagebox.showwarning("Đã bỏ qua", "APK đích không được thêm lại vào danh sách APK hệ thống.")

    def remove_system_apks(self):
        selected = list(self.dependency_list.curselection())
        for index in reversed(selected):
            del self.system_apks[index]
        self.refresh_dependency_list()

    def move_dependency(self, direction):
        selected = list(self.dependency_list.curselection())
        if len(selected) != 1:
            return
        old = selected[0]
        new = old + direction
        if new < 0 or new >= len(self.system_apks):
            return
        self.system_apks[old], self.system_apks[new] = self.system_apks[new], self.system_apks[old]
        self.refresh_dependency_list()
        self.dependency_list.selection_set(new)
        self.dependency_list.see(new)

    def refresh_dependency_list(self):
        self.dependency_list.delete(0, "end")
        for index, path in enumerate(self.system_apks, start=1):
            self.dependency_list.insert("end", f"{index:02d}. {path}")
        self.dependency_count.config(text=f"{len(self.system_apks)} file")

    def remove_duplicate_target_dependency(self):
        target = self.normalized_path(self.target_apk.get())
        if not target:
            return
        self.system_apks = [path for path in self.system_apks if self.normalized_path(path) != target]
        self.refresh_dependency_list()

    @staticmethod
    def normalized_path(path):
        if not path:
            return ""
        return os.path.normcase(os.path.abspath(path))

    def update_output_hint(self, *_):
        name = sanitize_output_name(self.output_name.get())
        self.output_hint.config(text=f"dtinh-{name}.apk")

    def open_output(self):
        OUT.mkdir(exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(OUT))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(OUT)])
            else:
                subprocess.Popen(["xdg-open", str(OUT)])
        except Exception as exc:
            messagebox.showerror("Lỗi", f"Không mở được thư mục output:\n{exc}")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def set_status(self, text, color=SUB, percent=None):
        self.root.after(0, lambda: self.status.config(text=text, fg=color))
        if percent is not None:
            def update_bar():
                width = max(self.progress.winfo_width(), 1)
                self.progress.coords(self.progress_bar, 0, 0, width * percent / 100, 4)
            self.root.after(0, update_bar)

    def logln(self, text):
        def add():
            line = text.rstrip()
            tag = None
            if line.startswith("✓"):
                tag = "success"
            elif line.startswith("✕"):
                tag = "error"
            elif line.startswith("•"):
                tag = "info"
            self.log.configure(state="normal")
            self.log.insert("end", line + "\n", tag)
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(0, add)

    def set_building(self, active):
        self.is_building = active
        self.root.after(0, lambda: self.build_button.config(state="disabled" if active else "normal"))

    @staticmethod
    def concise_process_error(stdout, stderr, secrets=()):
        text = "\n".join(part.strip() for part in (stderr, stdout) if part.strip())
        for secret in secrets:
            if secret:
                text = text.replace(secret, "***")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > 12:
            lines = lines[:3] + ["…"] + lines[-8:]
        return "\n".join(lines)

    def run(self, command, cwd=None, step=None):
        command = [str(part) for part in command]
        actual_command = command
        run_options = {}
        if os.name == "nt" and Path(command[0]).suffix.lower() in (".bat", ".cmd"):
            actual_command = ["cmd.exe", "/d", "/s", "/c", subprocess.list2cmdline(command)]
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            run_options["startupinfo"] = startupinfo
            run_options["creationflags"] = subprocess.CREATE_NO_WINDOW

        process = subprocess.run(
            actual_command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            **run_options,
        )
        if process.returncode:
            secrets = []
            for index, item in enumerate(command[:-1]):
                if item in ("--ks-pass", "--key-pass"):
                    secrets.append(command[index + 1])
            detail = self.concise_process_error(process.stdout, process.stderr, secrets)
            message = f"{step or Path(command[0]).name} thất bại (mã {process.returncode})"
            if detail:
                message += f"\n{detail}"
            raise RuntimeError(message)
        if step:
            self.logln(f"✓ {step}")
        return process.stdout

    def tool(self, name):
        candidates = (
            TOOLS / name,
            TOOLS / f"{name}.exe",
            TOOLS / f"{name}.bat",
            TOOLS / f"{name}.cmd",
        )
        for path in candidates:
            if path.exists():
                return path.resolve()
        found = shutil.which(name) or shutil.which(f"{name}.exe")
        if found:
            return Path(found).resolve()
        raise FileNotFoundError(f"Thiếu công cụ: {name}. Hãy kiểm tra thư mục tools.")

    @staticmethod
    def validate_apk_archive(path, label):
        if path.suffix.lower() == ".jar":
            if not zipfile.is_zipfile(path):
                raise RuntimeError(f"{label} không phải JAR hợp lệ: {path.name}")
            return
        if not zipfile.is_zipfile(path):
            raise RuntimeError(f"{label} không phải APK hợp lệ: {path.name}")
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        if "AndroidManifest.xml" not in names:
            raise RuntimeError(f"{label} thiếu AndroidManifest.xml: {path.name}")
        if "resources.arsc" not in names:
            raise RuntimeError(f"{label} không chứa resources.arsc: {path.name}")

    def collect_config(self):
        try:
            priority = int(self.priority.get().strip())
        except ValueError as exc:
            raise RuntimeError("Priority phải là số từ 0 đến 999") from exc

        return BuildConfig(
            target_apk=Path(self.target_apk.get()).expanduser(),
            translation_xmls=tuple(Path(path) for path in self.translation_xmls),
            system_apks=tuple(Path(path) for path in self.system_apks),
            keystore=Path(self.keystore.get()).expanduser(),
            alias=self.alias.get().strip(),
            keystore_password=self.keystore_password.get(),
            key_password=self.key_password.get(),
            output_name=sanitize_output_name(self.output_name.get()),
            overlay_package=self.overlay_package.get().strip(),
            priority=priority,
            add_base_and_chinese_values=bool(self.add_base_and_chinese_values.get()),
        )

    def validate_config(self, config):
        required = (
            (config.target_apk, "APK đích"),
            (config.keystore, "khóa ký"),
        )
        for path, label in required:
            if not path.is_file():
                raise RuntimeError(f"Chưa chọn hoặc không tìm thấy {label}")
        if not config.translation_xmls:
            raise RuntimeError("Chưa chọn file XML tiếng Việt")
        for translation_xml in config.translation_xmls:
            if not translation_xml.is_file():
                raise RuntimeError(f"Không tìm thấy file XML: {translation_xml}")
        for dependency in config.system_apks:
            if not dependency.is_file():
                raise RuntimeError(f"Không tìm thấy APK hệ thống: {dependency}")

        if not config.alias:
            raise RuntimeError("Chưa nhập alias khóa ký")
        if not config.keystore_password:
            raise RuntimeError("Chưa nhập mật khẩu keystore")
        if not 0 <= config.priority <= 999:
            raise RuntimeError("Priority phải nằm trong khoảng 0–999")
        if config.overlay_package and not PACKAGE_RE.fullmatch(config.overlay_package):
            raise RuntimeError("Overlay package không đúng định dạng package Android")

        target = self.normalized_path(config.target_apk)
        dependencies = [self.normalized_path(path) for path in config.system_apks]
        if target in dependencies:
            raise RuntimeError("APK đích đang bị chọn trùng trong danh sách APK hệ thống")
        if len(dependencies) != len(set(dependencies)):
            raise RuntimeError("Danh sách APK hệ thống có đường dẫn trùng")
        translation_xmls = [self.normalized_path(path) for path in config.translation_xmls]
        if len(translation_xmls) != len(set(translation_xmls)):
            raise RuntimeError("Danh sách XML có đường dẫn trùng")

    def start_build(self):
        if self.is_building:
            return
        try:
            config = self.collect_config()
            self.validate_config(config)
        except Exception as exc:
            messagebox.showerror("Thiếu thông tin", str(exc))
            return

        self.clear_log()
        self.logln("• Bắt đầu tạo overlay")
        self.set_building(True)
        threading.Thread(target=self.build, args=(config,), daemon=True).start()

    def detect_package(self, apk, aapt2):
        output = self.run([aapt2, "dump", "badging", apk], step="Đọc thông tin APK")
        match = re.search(r"package:\s+name='([^']+)'", output)
        if not match:
            raise RuntimeError("Không đọc được package của APK đích")
        return match.group(1)

    def build(self, config):
        temporary_path = None
        try:
            self.set_status("Kiểm tra dữ liệu…", BLUE, 3)
            aapt2 = self.tool("aapt2")
            zipalign = self.tool("zipalign")
            apksigner = self.tool("apksigner")
            android_jar = TOOLS / "android.jar"
            if not android_jar.is_file():
                raise FileNotFoundError("Thiếu tools/android.jar")

            target_apk = config.target_apk.resolve()
            translation_xmls = tuple(path.resolve() for path in config.translation_xmls)
            dependencies = tuple(path.resolve() for path in config.system_apks)
            keystore = config.keystore.resolve()

            self.validate_apk_archive(target_apk, "APK đích")
            for dependency in dependencies:
                self.validate_apk_archive(dependency, "APK hệ thống")

            target_package = self.detect_package(target_apk, aapt2)
            resource_entries = parse_resource_files(translation_xmls)
            overlay_package = config.overlay_package or automatic_overlay_package(
                target_package, config.output_name
            )
            if overlay_package == target_package:
                raise RuntimeError("Overlay package không được trùng package APK đích")

            temporary_path = Path(tempfile.mkdtemp(prefix="dtinh_overlay_", dir=WORK))
            staged_resources, resource_directories, format_fixes, reference_fixes = stage_overlay_project(
                temporary_path,
                translation_xmls,
                resource_entries,
                overlay_package,
                target_package,
                config.priority,
                config.add_base_and_chinese_values,
            )

            compiled = temporary_path / "compiled.zip"
            unsigned = temporary_path / "unsigned.apk"
            aligned = temporary_path / "aligned.apk"
            final_apk = OUT / f"dtinh-{config.output_name}.apk"

            counts = Counter(entry.resource_type for entry in resource_entries)
            type_summary = ", ".join(
                f"{resource_type}: {count}"
                for resource_type, count in sorted(counts.items())
            )
            self.logln(f"• APK đích: {target_package}")
            self.logln(f"• Package overlay: {overlay_package}")
            self.logln(
                f"• Resource: {len(resource_entries)} ({type_summary})"
            )
            self.logln(
                "• Thư mục: "
                + ", ".join(resource_directories)
            )
            self.logln(
                f"• Nguồn: {len(staged_resources)} XML, "
                f"{len(dependencies)} APK phụ thuộc"
            )
            if format_fixes:
                self.logln(
                    f"• Đã sửa formatted=\"false\": {len(format_fixes)} string"
                )
            if reference_fixes:
                self.logln(
                    f"• Đã định tuyến {len(reference_fixes)} resource reference"
                )

            self.set_status(
                f"Biên dịch {len(resource_entries)} resource…",
                BLUE,
                22,
            )
            self.run([
                aapt2,
                "compile",
                "--dir",
                temporary_path / "res",
                "-o",
                compiled,
            ], step="Biên dịch resource")

            self.set_status("Liên kết overlay…", BLUE, 52)
            link_command = make_link_command(
                aapt2,
                unsigned,
                temporary_path / "AndroidManifest.xml",
                android_jar,
                dependencies,
                target_apk,
                compiled,
            )
            self.run(link_command, step="Liên kết overlay")

            self.set_status("Căn chỉnh APK…", BLUE, 70)
            self.run(
                [zipalign, "-f", "4", unsigned, aligned],
                step="Căn chỉnh APK",
            )

            self.set_status("Ký APK…", BLUE, 84)
            final_apk.unlink(missing_ok=True)
            sign_command = [
                apksigner,
                "sign",
                "--ks",
                keystore,
                "--ks-key-alias",
                config.alias,
                "--ks-pass",
                f"pass:{config.keystore_password}",
                "--out",
                final_apk,
            ]
            if config.key_password:
                sign_command.extend(["--key-pass", f"pass:{config.key_password}"])
            sign_command.append(aligned)
            self.run(sign_command, step="Ký APK")
            self.run(
                [apksigner, "verify", "--verbose", final_apk],
                step="Xác minh chữ ký",
            )

            self.logln(f"✓ Hoàn tất: {final_apk.name}")
            self.set_status("Hoàn tất", SUCCESS, 100)

            message = f"Đã tạo thành công:\n{final_apk.name}"
            self.root.after(0, lambda: messagebox.showinfo("Hoàn tất", message))
        except Exception as exc:
            self.set_status("Có lỗi", ERROR, 0)
            self.logln(f"✕ {exc}")
            error_message = str(exc)
            self.root.after(
                0,
                lambda text=error_message: messagebox.showerror(
                    "Không thể tạo overlay", text
                ),
            )
        finally:
            if temporary_path:
                shutil.rmtree(temporary_path, ignore_errors=True)
            self.set_building(False)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
