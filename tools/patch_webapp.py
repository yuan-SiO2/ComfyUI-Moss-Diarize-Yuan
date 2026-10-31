#!/usr/bin/env python3
"""Turn an upstream web app checkout into a static export served at /yuancut.

Usage: python3 tools/patch_webapp.py /path/to/webapp-source
       YUANCUT_WORK=/path/to/webapp-source python3 tools/patch_webapp.py

The script is intentionally dumb and idempotent-ish: it rewrites a handful of
files, drops everything that needs a server, rebrands the UI and fails loudly
if an anchor it expects has moved (that is the signal to revisit it after an
upstream upgrade).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

BASE_PATH = "/yuancut"

if len(sys.argv) > 1:
    ROOT = Path(sys.argv[1]).expanduser().resolve()
else:
    ROOT = Path(os.environ["YUANCUT_WORK"]).expanduser().resolve()

WEB = ROOT / "apps" / "web"
SRC = WEB / "src"
EDITOR_DIR = SRC / "app" / "[locale]" / "editor"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"  wrote   {path.relative_to(ROOT)}")


def patch(path: Path, old: str, new: str, *, required: bool = True) -> None:
    text = path.read_text(encoding="utf-8")
    if new in text:
        print(f"  keep    {path.relative_to(ROOT)}")
        return
    if old not in text:
        if required:
            raise SystemExit(f"anchor not found in {path}:\n{old[:200]}")
        print(f"  skip    {path.relative_to(ROOT)}")
        return
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"  patched {path.relative_to(ROOT)}")


def patch_re(path: Path, pattern: str, new: str, *, expect: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    text2, count = re.subn(pattern, new, text, flags=re.S)
    if count == 0:
        print(f"  skip    {path.relative_to(ROOT)} (already applied)")
        return
    if count != expect:
        raise SystemExit(f"{path}: expected {expect} match(es), got {count}")
    path.write_text(text2, encoding="utf-8")
    print(f"  patched {path.relative_to(ROOT)}")


def drop_package_manager(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "packageManager" not in data:
        return
    data.pop("packageManager")
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  patched {path.relative_to(ROOT)} (dropped bun pin)")


print(f"Patching {ROOT} for static export under {BASE_PATH}")
if not WEB.is_dir():
    raise SystemExit(f"{WEB} does not exist — is this the YuanCut repo root?")

# --------------------------------------------------------------- package.json
# pnpm refuses to touch a workspace pinned to bun; the build machine may not
# have bun, so let pnpm drive the install.
drop_package_manager(ROOT / "package.json")
drop_package_manager(WEB / "package.json")

# ---------------------------------------------------------------- next.config
write(
    WEB / "next.config.ts",
    f"""import type {{ NextConfig }} from "next";

const basePath = "{BASE_PATH}";

const nextConfig: NextConfig = {{
\t// ComfyUI serves this whole export from custom_nodes, so no Node server.
\toutput: "export",
\tbasePath,
\ttrailingSlash: true,
\treactStrictMode: true,
\timages: {{
\t\tunoptimized: true,
\t}},
\tenv: {{
\t\tNEXT_PUBLIC_BASE_PATH: basePath,
\t}},
}};

export default nextConfig;
""",
)

# --------------------------------------------------------------- base path fix
write(
    SRC / "lib" / "base-path.ts",
    f"""/** ComfyUI serves the export from {BASE_PATH}, not from the site root. */
export const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

export function withBasePath(path: string): string {{
\tif (!path.startsWith("/")) return path;
\tif (!BASE_PATH || path === BASE_PATH || path.startsWith(`${{BASE_PATH}}/`)) {{
\t\treturn path;
\t}}
\treturn `${{BASE_PATH}}${{path}}`;
}}
""",
)

p = SRC / "constants" / "site-constants.ts"
if "withBasePath" not in p.read_text(encoding="utf-8"):
    text = p.read_text(encoding="utf-8")
    text = 'import { withBasePath } from "@/lib/base-path";\n\n' + text
    text = text.replace(
        'favicon: "/logos/cutia/svg/logo.svg",',
        'favicon: withBasePath("/logos/yuancut/svg/logo.svg"),',
    )
    text = text.replace(
        'export const DEFAULT_LOGO_URL = "/logos/cutia/svg/logo.svg";',
        'export const DEFAULT_LOGO_URL = withBasePath("/logos/yuancut/svg/logo.svg");',
    )
    p.write_text(text, encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)}")

p = SRC / "components" / "editor" / "layout-guide-overlay.tsx"
if "withBasePath" not in p.read_text(encoding="utf-8"):
    patch(
        p,
        'import Image from "next/image";',
        'import Image from "next/image";\nimport { withBasePath } from "@/lib/base-path";',
    )
    patch(
        p,
        'src="/platform-guides/tiktok-blueprint.png"',
        'src={withBasePath("/platform-guides/tiktok-blueprint.png")}',
    )

p = SRC / "app" / "metadata.ts"
if "withBasePath" not in p.read_text(encoding="utf-8"):
    text = p.read_text(encoding="utf-8")
    text = 'import { withBasePath } from "@/lib/base-path";\n' + text
    text = text.replace(
        'url: "/logos/cutia/svg/logo.svg"',
        'url: withBasePath("/logos/yuancut/svg/logo.svg")',
    )
    text = text.replace(
        'shortcut: ["/logos/cutia/svg/logo.svg"]',
        'shortcut: [withBasePath("/logos/yuancut/svg/logo.svg")]',
    )
    text = text.replace('manifest: "/manifest.json"', 'manifest: withBasePath("/manifest.json")')
    p.write_text(text, encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)}")

p = WEB / "public" / "manifest.json"
raw = p.read_text(encoding="utf-8")
if '"/yuancut/logos/' not in raw:
    raw = raw.replace('"/logos/', '"/yuancut/logos/').replace('"/icons/', '"/yuancut/icons/')
    p.write_text(raw, encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)}")

# ------------------------------------------------------------------ root layout
p = SRC / "app" / "[locale]" / "layout.tsx"
text = p.read_text(encoding="utf-8")
if "BotIdClient" in text:
    text = text.replace('import Script from "next/script";\n', "")
    text = text.replace('import { BotIdClient } from "botid/client";\n', "")
    text = re.sub(
        r"\t\t\t\t<BotIdClient protect=\{protectedRoutes\} />\n\t\t\t\t<Script\n.*?/>\n",
        "",
        text,
        flags=re.S,
    )
    text = re.sub(r"const protectedRoutes = \[.*?\];\n\n", "", text, flags=re.S)
    p.write_text(text, encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)} (no botid / analytics script)")

# -------------------------------------------------------- editor: query route
# Static export cannot prerender /editor/<uuid>, so the project id moves to
# ?project=<uuid> and the route becomes a plain static page.
old_page = EDITOR_DIR / "[project_id]" / "page.tsx"
new_page = EDITOR_DIR / "page.tsx"
if old_page.is_file():
    text = old_page.read_text(encoding="utf-8")
    text = text.replace(
        'import { useParams } from "next/navigation";',
        'import { useSearchParams } from "next/navigation";',
    )
    entry = """export default function Editor() {
\treturn (
\t\t<Suspense fallback={<EditorLoading />}>
\t\t\t<EditorWithProject />
\t\t</Suspense>
\t);
}

function EditorWithProject() {
\tconst searchParams = useSearchParams();
\tconst projectId = searchParams.get("project");

\treturn (
\t\t<EditorProvider projectId={projectId}>
\t\t\t<EditorShell />
\t\t</EditorProvider>
\t);
}

function EditorLoading() {
\treturn (
\t\t<div className="bg-background flex h-screen w-screen items-center justify-center">
\t\t\t<div className="flex flex-col items-center gap-4">
\t\t\t\t<p className="text-muted-foreground text-sm">正在加载编辑器…</p>
\t\t\t</div>
\t\t</div>
\t);
}
"""
    text, count = re.subn(
        r"export default function Editor\(\) \{.*?\n\}\n\nfunction EditorShell",
        entry + "\nfunction EditorShell",
        text,
        flags=re.S,
    )
    if count != 1:
        raise SystemExit("could not rewrite the editor entry point")
    write(new_page, text)

# ------------------------------------------------------------------- delete
for rel in (
    "src/app/api",
    "src/middleware.ts",
    "src/app/[locale]/editor/[project_id]",
    # server-only code that no longer has a caller (and does not type check
    # against the newer better-auth / drizzle versions)
    "src/lib/auth",
    "src/lib/db",
    "src/lib/r2",
    "src/lib/rate-limit.ts",
):
    target = WEB / rel
    if target.is_dir():
        shutil.rmtree(target)
        print(f"  removed {rel}/")
    elif target.exists():
        target.unlink()
        print(f"  removed {rel}")

# ------------------------------------------------------------ editor provider
p = SRC / "components" / "providers" / "editor-provider.tsx"
text = p.read_text(encoding="utf-8")
if "createProject" not in text:
    text = text.replace("\tprojectId: string;\n", "\tprojectId: string | null;\n", 1)
    old_effect = """\t\tconst loadProject = async () => {
\t\t\ttry {
\t\t\t\tsetIsLoading(true);
\t\t\t\tawait editor.project.loadProject({ id: projectId });
"""
    new_effect = """\t\tconst createProject = async () => {
\t\t\tconst newProjectId = await editor.project.createNewProject({
\t\t\t\tname: "未命名项目",
\t\t\t});
\t\t\tif (cancelled) return;
\t\t\trouter.replace(`/editor?project=${newProjectId}`);
\t\t};

\t\tconst loadProject = async () => {
\t\t\ttry {
\t\t\t\tsetIsLoading(true);

\t\t\t\tif (!projectId) {
\t\t\t\t\tawait createProject();
\t\t\t\t\treturn;
\t\t\t\t}

\t\t\t\tawait editor.project.loadProject({ id: projectId });
"""
    if old_effect not in text:
        raise SystemExit("could not find the editor provider's load effect")
    text = text.replace(old_effect, new_effect, 1)
    old_not_found = """\t\t\t\tif (isNotFound) {
\t\t\t\t\ttry {
\t\t\t\t\t\tconst newProjectId = await editor.project.createNewProject({
\t\t\t\t\t\t\tname: "Untitled Project",
\t\t\t\t\t\t});
\t\t\t\t\t\trouter.replace(`/editor/${newProjectId}`);
\t\t\t\t\t} catch (_createErr) {"""
    new_not_found = """\t\t\t\tif (isNotFound) {
\t\t\t\t\ttry {
\t\t\t\t\t\tawait createProject();
\t\t\t\t\t} catch (_createErr) {"""
    if old_not_found not in text:
        raise SystemExit("could not find the editor provider's not-found branch")
    text = text.replace(old_not_found, new_not_found, 1)
    p.write_text(text, encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)} (query based project id)")

# ------------------------------------------------------- 中文文案（硬编码部分）
# 上游少量不走 i18n 的英文文案，插件里统一显示中文。
p = SRC / "components" / "providers" / "editor-provider.tsx"
text = p.read_text(encoding="utf-8")
zh_fixes = {
    '"Failed to create project"': '"创建项目失败"',
    '"Failed to load project"': '"加载项目失败"',
    ">Loading project...<": ">正在加载项目…<",
    ">Exiting project...<": ">正在退出项目…<",
    '"Untitled Project"': '"未命名项目"',
}
changed = 0
for old, new in zh_fixes.items():
    if old in text:
        text = text.replace(old, new)
        changed += 1
p.write_text(text, encoding="utf-8")
print(f"  localized {changed} hardcoded string(s) in editor-provider")

# ------------------------------------------------- 中文文案（编辑器里的零散英文）
# 上游大部分文案走 t()，这里补齐没走 i18n 的提示、tooltip 与无障碍标签。
ZH_ATTR = {
    "Timeline ruler": "时间轴刻度尺",
    "Timeline playhead": "时间轴播放头",
    "Timeline": "时间轴",
    "Toggle Sidebar": "切换侧边栏",
    "Project thumbnail": "项目缩略图",
    "TikTok layout guide": "TikTok 构图参考线",
    "DELETE": "删除",
    "Close preview": "关闭预览",
    "Name": "名称",
    "Add transition": "添加转场",
    "Resize transition left": "向左调整转场",
    "Resize transition right": "向右调整转场",
    "AI generated result": "AI 生成结果",
    "AI generated": "AI 生成",
    "Select a font": "选择字体",
    "Clear input": "清空输入",
    "Go to previous page": "上一页",
    "Go to next page": "下一页",
    "Search country...": "搜索国家…",
    "Loading": "加载中",
}
ZH_TEXT = {
    "Loading...": "加载中…",
    "Close": "关闭",
    "Effects view coming soon...": "特效面板即将上线…",
    "Filters view coming soon...": "滤镜面板即将上线…",
    "Adjustment view coming soon...": "调整面板即将上线…",
    "Alignment guides": "对齐参考线",
    "Clear All": "全部清除",
    "Audio unavailable": "音频不可用",
    "Transitions": "转场",
    "Remove transition": "移除转场",
    "Transition": "转场",
    "Add transition": "添加转场",
    "Added": "已添加",
    "Retry": "重试",
    "More": "更多",
    "Previous": "上一页",
    "Next": "下一页",
    "Previous slide": "上一张",
    "Next slide": "下一张",
    "More pages": "更多页",
    "No country found.": "未找到国家",
    "Sidebar": "侧边栏",
    "Displays the mobile sidebar.": "显示移动端侧边栏。",
    "Toggle Sidebar": "切换侧边栏",
    "Switch language": "切换语言",
}
localized_files = 0
for path in sorted(SRC.rglob("*.tsx")):
    s = path.read_text(encoding="utf-8")
    original = s
    for en, zh in ZH_ATTR.items():
        for attr in ("aria-label", "title", "placeholder", "alt"):
            s = s.replace(f'{attr}="{en}"', f'{attr}="{zh}"')
    for en, zh in ZH_TEXT.items():
        s = re.sub(
            rf">(\s*){re.escape(en)}(\s*)<",
            lambda m, zh=zh: f">{m.group(1)}{zh}{m.group(2)}<",
            s,
        )
    if s != original:
        path.write_text(s, encoding="utf-8")
        localized_files += 1
print(f"  localized {localized_files} tsx file(s) with Chinese strings")

# 少量写在表达式里的英文文案（title 属性、主题切换读屏文字）
ZH_LITERAL = {
    '"Click to edit time"': '"点击编辑时间"',
    '"Enter fullscreen"': '"进入全屏"',
    '"Exit fullscreen"': '"退出全屏"',
    '"Light"': '"浅色"',
    '"Dark"': '"深色"',
}
for path in sorted(SRC.rglob("*.tsx")):
    s = path.read_text(encoding="utf-8")
    fixed = s
    for en, zh in ZH_LITERAL.items():
        fixed = fixed.replace(en, zh)
    if fixed != s:
        path.write_text(fixed, encoding="utf-8")

# 通知区域的读屏标签（sonner 默认是英文 "Notifications alt+T"）
p = SRC / "components" / "ui" / "sonner.tsx"
text = p.read_text(encoding="utf-8")
if "containerAriaLabel" not in text:
    text = text.replace("<Sonner\n", '<Sonner\n\t\t\tcontainerAriaLabel="通知"\n', 1)
    p.write_text(text, encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)} (toast aria label)")

# 项目列表的日期格式（上游硬编码 en-US）
p = SRC / "utils" / "date.ts"
text = p.read_text(encoding="utf-8")
if '"en-US"' in text:
    p.write_text(text.replace('"en-US"', '"zh-CN"'), encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)} (zh-CN dates)")

# ------------------------------------------- 素材并发加载（原本是逐个串行）
# 项目里素材多时，串行读 IndexedDB 会让「进入项目」明显变慢。
p = SRC / "services" / "storage" / "service.ts"
text = p.read_text(encoding="utf-8")
old_loop = """\t\tconst mediaIds = await mediaMetadataAdapter.list();
\t\tconst mediaItems: MediaAsset[] = [];

\t\tfor (const id of mediaIds) {
\t\t\tconst item = await this.loadMediaAsset({ projectId, id });
\t\t\tif (item) {
\t\t\t\tmediaItems.push(item);
\t\t\t}
\t\t}
"""
new_loop = """\t\tconst mediaIds = await mediaMetadataAdapter.list();
\t\tconst mediaItems: MediaAsset[] = [];

\t\t// 有上限的并发加载：文件多时比逐个 await 快数倍，又不会一次性读爆内存
\t\tconst concurrency = 4;
\t\tfor (let i = 0; i < mediaIds.length; i += concurrency) {
\t\t\tconst batch = await Promise.all(
\t\t\t\tmediaIds
\t\t\t\t\t.slice(i, i + concurrency)
\t\t\t\t\t.map((id) => this.loadMediaAsset({ projectId, id })),
\t\t\t);
\t\t\tfor (const item of batch) {
\t\t\t\tif (item) mediaItems.push(item);
\t\t\t}
\t\t}
"""
if old_loop in text:
    p.write_text(text.replace(old_loop, new_loop, 1), encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)} (parallel media load)")

# ------------------------------------------------ 缩略图不再阻塞进入 / 退出
p = SRC / "core" / "managers" / "project-manager.ts"
text = p.read_text(encoding="utf-8")

old_load = """\t\t\tif (!project.metadata.thumbnail) {
\t\t\t\tconst didUpdateThumbnail = await this.updateThumbnailFromTimeline();
\t\t\t\tif (didUpdateThumbnail) {
\t\t\t\t\tawait this.saveCurrentProject();
\t\t\t\t}
\t\t\t}
"""
new_load = """\t\t\tif (!project.metadata.thumbnail) {
\t\t\t\t// 缩略图放后台画，不阻塞编辑器显示（视频项目渲染可能要好几秒）
\t\t\t\tvoid this.updateThumbnailFromTimeline()
\t\t\t\t\t.then(async (didUpdateThumbnail) => {
\t\t\t\t\t\tif (didUpdateThumbnail) {
\t\t\t\t\t\t\tawait this.saveCurrentProject();
\t\t\t\t\t\t}
\t\t\t\t\t})
\t\t\t\t\t.catch(() => {});
\t\t\t}
"""
if old_load in text:
    text = text.replace(old_load, new_load, 1)
    print(f"  patched {p.relative_to(ROOT)} (non-blocking thumbnail on load)")

old_exit = """\tasync prepareExit(): Promise<void> {
\t\tif (!this.active) return;

\t\ttry {
\t\t\tconst didUpdateThumbnail = await this.updateThumbnailFromTimeline();
\t\t\tif (didUpdateThumbnail) {
\t\t\t\tawait this.editor.save.flush();
\t\t\t}
\t\t} catch (error) {
\t\t\tconsole.error(\"Failed to generate project thumbnail on exit:\", error);
\t\t}
\t}
"""
new_exit = """\tasync prepareExit(): Promise<void> {
\t\tif (!this.active) return;

\t\t// 视频项目画缩略图要解码视频/建 GPU 渲染器，退出时直接跳过，保证秒退
\t\tif (
\t\t\tthis.editor.media.getAssets().some((asset) => asset.type === "video")
\t\t) {
\t\t\treturn;
\t\t}

\t\tconst thumbnailTask = this.updateThumbnailFromTimeline()
\t\t\t.then(async (didUpdateThumbnail) => {
\t\t\t\tif (didUpdateThumbnail) {
\t\t\t\t\tawait this.editor.save.flush();
\t\t\t\t}
\t\t\t})
\t\t\t.catch(() => {
\t\t\t\t// 缩略图只是锦上添花，失败不影响退出
\t\t\t});

\t\t// 退出最多等 200ms：素材多/视频项目渲染慢时直接走，不让人干等
\t\tawait Promise.race([
\t\t\tthumbnailTask,
\t\t\tnew Promise<void>((resolve) => setTimeout(resolve, 200)),
\t\t]);
\t}
"""
if old_exit in text:
    text = text.replace(old_exit, new_exit, 1)
    print(f"  patched {p.relative_to(ROOT)} (bounded thumbnail on exit)")

else:
    raise SystemExit("prepareExit anchor not found")

p.write_text(text, encoding="utf-8")

# ---------------------------------------------------- 默认三条轨道 + 轨道排序
# 结构（上 → 下）：文字/特效轨道 → 副视频轨道 → 主视频轨道 → 音频轨道
p = SRC / "lib" / "timeline" / "track-utils.ts"
text = p.read_text(encoding="utf-8")

for old, new in (
    ('? "Video track"', '? "视频轨道"'),
    ('? "Text track"', '? "文字 / 特效"'),
    ('? "Audio track"', '? "音频轨道"'),
    ('? "Sticker track"', '? "贴纸轨道"'),
    ('name: "Main Track",', 'name: "主视频",'),
):
    if old in text:
        text = text.replace(old, new, 1)

old_insert = '''export function getDefaultInsertIndexForTrack({
	tracks,
	trackType,
}: {
	tracks: TimelineTrack[];
	trackType: TrackType;
}): number {
	if (trackType === "audio") {
		return tracks.length;
	}

	const mainTrackIndex = tracks.findIndex((track) => isMainTrack(track));
	if (mainTrackIndex >= 0) {
		return mainTrackIndex;
	}

	const firstAudioTrackIndex = tracks.findIndex(
		(track) => track.type === "audio",
	);
	if (firstAudioTrackIndex >= 0) {
		return firstAudioTrackIndex;
	}

	return tracks.length;
}
'''
new_insert = '''export function getDefaultInsertIndexForTrack({
	tracks,
	trackType,
}: {
	tracks: TimelineTrack[];
	trackType: TrackType;
}): number {
	// 音频/音效只能往下加：永远放到最底部
	if (trackType === "audio") {
		return tracks.length;
	}

	// 文字/特效/贴纸只能往上加：永远放到最顶层
	if (trackType === "text" || trackType === "sticker") {
		return 0;
	}

	// 视频轨道贴着主视频轨道：插在主轨道正上方
	const mainTrackIndex = tracks.findIndex((track) => isMainTrack(track));
	return mainTrackIndex >= 0 ? mainTrackIndex : tracks.length;
}

const TRACK_ORDER_RANK: Record<TrackType, number> = {
	text: 0,
	sticker: 0,
	video: 1,
	audio: 3,
};

/**
 * 把轨道整理成固定顺序（上 → 下）：
 * 文字/特效/贴纸 → 副视频 → 主视频 → 音频。
 * 已经有序时返回原数组，避免触发多余的保存。
 */
export function normalizeTrackOrder({
	tracks,
}: {
	tracks: TimelineTrack[];
}): TimelineTrack[] {
	const ranked = tracks
		.map((track, index) => ({
			track,
			index,
			rank: isMainTrack(track) ? 2 : TRACK_ORDER_RANK[track.type],
		}))
		.sort((a, b) => a.rank - b.rank || a.index - b.index)
		.map(({ track }) => track);

	const changed = ranked.some((track, index) => track.id !== tracks[index]?.id);
	return changed ? ranked : tracks;
}

/** 新项目的初始轨道：文字/特效、主视频、音频 */
export function buildDefaultTracks(): TimelineTrack[] {
	const textTrack = buildEmptyTrack({
		id: generateUUID(),
		type: "text",
		name: "文字 / 特效",
	});
	const videoTrack = buildEmptyTrack({
		id: generateUUID(),
		type: "video",
		name: "主视频",
	});
	if (videoTrack.type === "video") {
		videoTrack.isMain = true;
	}
	const audioTrack = buildEmptyTrack({
		id: generateUUID(),
		type: "audio",
		name: "音频",
	});
	return [textTrack, videoTrack, audioTrack];
}
'''
if old_insert not in text:
    raise SystemExit("getDefaultInsertIndexForTrack anchor not found")
text = text.replace(old_insert, new_insert, 1)
p.write_text(text, encoding="utf-8")
print(f"  patched {p.relative_to(ROOT)} (default tracks + order rules)")

# 新场景直接生成三条轨道
p = SRC / "lib" / "scenes.ts"
text = p.read_text(encoding="utf-8")
text = text.replace(
    'import { ensureMainTrack } from "@/lib/timeline/track-utils";',
    'import { buildDefaultTracks } from "@/lib/timeline/track-utils";',
    1,
)
text = text.replace(
    "const tracks = ensureMainTrack({ tracks: [] });",
    "const tracks = buildDefaultTracks();",
    1,
)
text = text.replace(
    'buildDefaultScene({ name: "Main scene", isMain: true })',
    'buildDefaultScene({ name: "主场景", isMain: true })',
    1,
)
p.write_text(text, encoding="utf-8")
print(f"  patched {p.relative_to(ROOT)} (three default tracks)")

# 所有轨道写入都过一遍排序，保证不穿插
p = SRC / "core" / "managers" / "timeline-manager.ts"
text = p.read_text(encoding="utf-8")
text = text.replace(
    'import { calculateTotalDuration } from "@/lib/timeline";',
    'import { calculateTotalDuration, normalizeTrackOrder } from "@/lib/timeline";',
    1,
)
old_update = '''	updateTracks(newTracks: TimelineTrack[]): void {
		this.editor.scenes.updateSceneTracks({ tracks: newTracks });
		this.notify();
	}
'''
new_update = '''	updateTracks(newTracks: TimelineTrack[]): void {
		this.editor.scenes.updateSceneTracks({
			tracks: normalizeTrackOrder({ tracks: newTracks }),
		});
		this.notify();
	}
'''
if old_update not in text:
    raise SystemExit("updateTracks anchor not found")
p.write_text(text.replace(old_update, new_update, 1), encoding="utf-8")
print(f"  patched {p.relative_to(ROOT)} (normalize on write)")

# 打开旧项目时也整理一次顺序
p = SRC / "core" / "managers" / "scenes-manager.ts"
text = p.read_text(encoding="utf-8")
text = text.replace(
    'import { ensureMainTrack } from "@/lib/timeline/track-utils";',
    'import { ensureMainTrack, normalizeTrackOrder } from "@/lib/timeline/track-utils";',
    1,
)
old_scene = '''			const existingTracks = scene.tracks ?? [];
			const updatedTracks = ensureMainTrack({ tracks: existingTracks });
			if (updatedTracks !== existingTracks) {
'''
new_scene = '''			const existingTracks = scene.tracks ?? [];
			const updatedTracks = normalizeTrackOrder({
				tracks: ensureMainTrack({ tracks: existingTracks }),
			});
			if (updatedTracks !== existingTracks) {
'''
if old_scene not in text:
    raise SystemExit("scenes-manager anchor not found")
p.write_text(text.replace(old_scene, new_scene, 1), encoding="utf-8")
print(f"  patched {p.relative_to(ROOT)} (normalize on load)")

# 主场景名称
p = SRC / "core" / "managers" / "project-manager.ts"
text = p.read_text(encoding="utf-8")
text = text.replace(
    'buildDefaultScene({ name: "Main scene", isMain: true })',
    'buildDefaultScene({ name: "主场景", isMain: true })',
    1,
)
p.write_text(text, encoding="utf-8")
print(f"  patched {p.relative_to(ROOT)} (scene name)")

# -------------------------------------------- 三条基础轨道保护 + 自动补齐
p = SRC / "lib" / "timeline" / "track-utils.ts"
text = p.read_text(encoding="utf-8")
if "export function ensureBaseTracks" not in text:
    text += """

export function ensureBaseTracks({
	tracks,
}: {
	tracks: TimelineTrack[];
}): TimelineTrack[] {
	const hasMain = tracks.some((track) => isMainTrack(track));
	const hasText = tracks.some(
		(track) => track.type === "text" || track.type === "sticker",
	);
	const hasAudio = tracks.some((track) => track.type === "audio");

	let next = tracks;

	if (!hasMain) {
		const mainTrack = buildEmptyTrack({
			id: generateUUID(),
			type: "video",
			name: "主视频",
		});
		if (mainTrack.type === "video") {
			mainTrack.isMain = true;
		}
		next = [...next, mainTrack];
	}
	if (!hasText) {
		next = [
			buildEmptyTrack({ id: generateUUID(), type: "text", name: "文字 / 特效" }),
			...next,
		];
	}
	if (!hasAudio) {
		next = [
			...next,
			buildEmptyTrack({ id: generateUUID(), type: "audio", name: "音频" }),
		];
	}

	return next.length === tracks.length ? tracks : next;
}

/** 基础轨道（主视频、唯一的文字轨、唯一的音频轨）不允许删除 */
export function canRemoveTrack({
	tracks,
	trackId,
}: {
	tracks: TimelineTrack[];
	trackId: string;
}): boolean {
	const target = tracks.find((track) => track.id === trackId);
	if (!target) return false;
	if (isMainTrack(target)) return false;
	if (target.type === "text" || target.type === "sticker") {
		return (
			tracks.filter((track) => track.type === "text" || track.type === "sticker")
				.length > 1
		);
	}
	if (target.type === "audio") {
		return tracks.filter((track) => track.type === "audio").length > 1;
	}
	return true;
}
"""
    p.write_text(text, encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)} (base tracks guard)")

# 每次写轨道都补齐基础轨道，避免掉到只剩一条
p = SRC / "core" / "managers" / "timeline-manager.ts"
text = p.read_text(encoding="utf-8")
text = text.replace(
    'import { calculateTotalDuration, normalizeTrackOrder } from "@/lib/timeline";',
    'import { calculateTotalDuration, ensureBaseTracks, normalizeTrackOrder } from "@/lib/timeline";',
    1,
)
old_update = """	updateTracks(newTracks: TimelineTrack[]): void {
		this.editor.scenes.updateSceneTracks({
			tracks: normalizeTrackOrder({ tracks: newTracks }),
		});
		this.notify();
	}
"""
new_update = """	updateTracks(newTracks: TimelineTrack[]): void {
		const withBaseTracks = ensureBaseTracks({
			tracks: normalizeTrackOrder({ tracks: newTracks }),
		});
		this.editor.scenes.updateSceneTracks({
			tracks: normalizeTrackOrder({ tracks: withBaseTracks }),
		});
		this.notify();
	}
"""
if old_update not in text:
    raise SystemExit("updateTracks anchor (2) not found")
p.write_text(text.replace(old_update, new_update, 1), encoding="utf-8")
print(f"  patched {p.relative_to(ROOT)} (ensure base tracks on write)")

# 载入项目时补齐基础轨道（旧项目也会变成三条）
p = SRC / "core" / "managers" / "scenes-manager.ts"
text = p.read_text(encoding="utf-8")
text = text.replace(
    'import { ensureMainTrack, normalizeTrackOrder } from "@/lib/timeline/track-utils";',
    'import { ensureBaseTracks, normalizeTrackOrder } from "@/lib/timeline/track-utils";',
    1,
)
text = text.replace(
    "tracks: ensureMainTrack({ tracks: existingTracks }),",
    "tracks: ensureBaseTracks({ tracks: existingTracks }),",
    1,
)
p.write_text(text, encoding="utf-8")
print(f"  patched {p.relative_to(ROOT)} (ensure base tracks on load)")

# 删除轨道：基础轨道禁用
p = SRC / "components" / "editor" / "panels" / "timeline" / "index.tsx"
text = p.read_text(encoding="utf-8")
text = text.replace(
    "\tisMainTrack,\n} from \"@/lib/timeline\";",
    "\tisMainTrack,\n\tcanRemoveTrack,\n} from \"@/lib/timeline\";",
    1,
)
text = text.replace(
    "disabled={isMainTrack(track)}",
    "disabled={!canRemoveTrack({ tracks, trackId: track.id })}",
    1,
)
p.write_text(text, encoding="utf-8")
print(f"  patched {p.relative_to(ROOT)} (disable delete on base tracks)")

# 命令层再兜一层，防止其它入口删掉基础轨道
p = SRC / "lib" / "commands" / "timeline" / "track" / "remove-track.ts"
text = p.read_text(encoding="utf-8")
text = text.replace(
    'import { getMainTrack } from "@/lib/timeline";',
    'import { canRemoveTrack, getMainTrack } from "@/lib/timeline";',
    1,
)
old_cmd = """		if (mainTrack?.id === targetTrack?.id) {
			return;
		}
"""
new_cmd = """		if (mainTrack?.id === targetTrack?.id) {
			return;
		}
		if (!canRemoveTrack({ tracks: this.savedState, trackId: this.trackId })) {
			return;
		}
"""
if old_cmd not in text:
    raise SystemExit("remove-track anchor not found")
p.write_text(text.replace(old_cmd, new_cmd, 1), encoding="utf-8")
print(f"  patched {p.relative_to(ROOT)} (command guard)")

# ---------------------------------------------------- 时间轴默认缩放到 5 秒一格
# 50px/秒（zoom 1）时标尺每 1 秒一个标签，看起来太密；30px/秒时
# 标签间距 150px，正好是 5 秒一格，中间保留 1 秒细刻度。
p = SRC / "constants" / "timeline-constants.tsx"
text = p.read_text(encoding="utf-8")
old_view = """export const DEFAULT_TIMELINE_VIEW_STATE: TTimelineViewState = {
	zoomLevel: 1,
	scrollLeft: 0,
	playheadTime: 0,
};"""
new_view = """export const DEFAULT_TIMELINE_VIEW_STATE: TTimelineViewState = {
	// 30px/秒：初始标尺 5 秒一格（150px 标签间距），中间 1 秒细刻度
	zoomLevel: 0.6,
	scrollLeft: 0,
	playheadTime: 0,
};"""
if old_view in text:
    p.write_text(text.replace(old_view, new_view, 1), encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)} (default zoom 0.6 = 5s per label)")
else:
    print(f"  skip {p.relative_to(ROOT)} (default zoom already set)")

# 空/短时间轴也要能缩到「几秒一格」：最小缩放按至少 30 秒的可视范围来算
p = SRC / "lib" / "timeline" / "zoom-utils.ts"
text = p.read_text(encoding="utf-8")
old_zoom = "\tconst safeDuration = Math.max(duration, 1);"
new_zoom = """\t// 至少按 30 秒的可视范围计算，否则空时间轴的最小缩放会非常大，
\t// 导致刻度只能停在「帧/0.5 秒」级别，没法缩到 5 秒一格
\tconst safeDuration = Math.max(duration, 30);"""
if old_zoom in text:
    p.write_text(text.replace(old_zoom, new_zoom, 1), encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)} (min zoom for short timelines)")

# ------------------------------------------------ 时间轴刻度版本（一次性迁移）
p = SRC / "types" / "project.ts"
text = p.read_text(encoding="utf-8")
old_type = "export interface TTimelineViewState {\n\tzoomLevel: number;\n\tscrollLeft: number;\n\tplayheadTime: number;\n}"
new_type = "export interface TTimelineViewState {\n\tzoomLevel: number;\n\tscrollLeft: number;\n\tplayheadTime: number;\n\t/** 刻度方案版本：升级后把旧工程的缩放重置成 5 秒一格 */\n\tscaleVersion?: number;\n}"
if old_type in text:
    p.write_text(text.replace(old_type, new_type, 1), encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)} (view state version)")

# 2) 默认视图状态带版本
p = SRC / "constants" / "timeline-constants.tsx"
text = p.read_text(encoding="utf-8")
if "TIMELINE_SCALE_VERSION" not in text:
    text = text.replace(
        "export const DEFAULT_TIMELINE_VIEW_STATE",
        "export const TIMELINE_SCALE_VERSION = 2;\n\nexport const DEFAULT_TIMELINE_VIEW_STATE",
        1,
    )
    text = text.replace(
        "\tzoomLevel: 0.6,\n\tscrollLeft: 0,\n\tplayheadTime: 0,",
        "\tzoomLevel: 0.6,\n\tscrollLeft: 0,\n\tplayheadTime: 0,\n\tscaleVersion: TIMELINE_SCALE_VERSION,",
        1,
    )
    p.write_text(text, encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)} (scale version constant)")

# 3) 读取时迁移 + 写入时打版本
p = SRC / "core" / "managers" / "project-manager.ts"
text = p.read_text(encoding="utf-8")
text = text.replace(
    'import { DEFAULT_TIMELINE_VIEW_STATE } from "@/constants/timeline-constants";',
    'import {\n\tDEFAULT_TIMELINE_VIEW_STATE,\n\tTIMELINE_SCALE_VERSION,\n} from "@/constants/timeline-constants";',
    1,
)
old_get = """\tgetTimelineViewState(): TTimelineViewState {
\t\treturn this.active?.timelineViewState ?? DEFAULT_TIMELINE_VIEW_STATE;
\t}
"""
new_get = """\tgetTimelineViewState(): TTimelineViewState {
\t\tconst viewState =
\t\t\tthis.active?.timelineViewState ?? DEFAULT_TIMELINE_VIEW_STATE;
\t\t// 旧视图状态没有刻度版本：重置为默认的 5 秒一格（一次性迁移）
\t\tif ((viewState.scaleVersion ?? 1) < TIMELINE_SCALE_VERSION) {
\t\t\treturn {
\t\t\t\t...viewState,
\t\t\t\tzoomLevel: DEFAULT_TIMELINE_VIEW_STATE.zoomLevel,
\t\t\t\tscaleVersion: TIMELINE_SCALE_VERSION,
\t\t\t};
\t\t}
\t\treturn viewState;
\t}
"""
if old_get not in text:
    raise SystemExit("getTimelineViewState anchor not found")
text = text.replace(old_get, new_get, 1)
text = text.replace(
    "\t\t\ttimelineViewState: viewState ?? undefined,",
    "\t\t\ttimelineViewState: viewState\n\t\t\t\t? { ...viewState, scaleVersion: TIMELINE_SCALE_VERSION }\n\t\t\t\t: undefined,",
    1,
)
p.write_text(text, encoding="utf-8")
print(f"  patched {p.relative_to(ROOT)} (reset + persist scale version)")

# ------------------------------------------------ 音频导出 / 合并
write(
    SRC / "lib" / "audio-export.ts",
    """import { toast } from "sonner";
import type { EditorCore } from "@/core";
import type { AudioElement, AudioTrack, TimelineTrack } from "@/types/timeline";
import { extractTimelineAudio } from "@/lib/media/mediabunny";
import { buildUploadAudioElement } from "@/lib/timeline";

type SelectedAudio = { track: AudioTrack; element: AudioElement };

function collectSelectedAudio({
	editor,
}: {
	editor: EditorCore;
}): SelectedAudio[] {
	const tracks = editor.timeline.getTracks();
	const items: SelectedAudio[] = [];

	for (const { trackId, elementId } of editor.selection.getSelectedElements()) {
		const track = tracks.find((item) => item.id === trackId);
		if (!track || track.type !== "audio") continue;
		const element = track.elements.find((item) => item.id === elementId);
		if (!element) continue;
		items.push({ track, element });
	}

	return items.sort((a, b) => a.element.startTime - b.element.startTime);
}

function sanitizeFilename({ name }: { name: string }): string {
	return name.replaceAll("/", "_").trim() || "audio";
}

function downloadBlob({ blob, filename }: { blob: Blob; filename: string }): void {
	const url = URL.createObjectURL(blob);
	const link = document.createElement("a");
	link.href = url;
	link.download = filename;
	document.body.appendChild(link);
	link.click();
	link.remove();
	window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** 选中音频右键导出：单个直接导出，多个按时间从左到右编号导出 */
export async function exportSelectedAudio({
	editor,
}: {
	editor: EditorCore;
}): Promise<void> {
	const items = collectSelectedAudio({ editor });
	if (items.length === 0) {
		toast.error("请先选中音频片段");
		return;
	}

	const mediaAssets = editor.media.getAssets();
	const multiple = items.length > 1;
	const toastId = "export-selected-audio";
	toast.loading("正在导出音频…", { id: toastId });

	try {
		for (let index = 0; index < items.length; index += 1) {
			const { track, element } = items[index];
			const renderTrack = {
				...track,
				muted: false,
				elements: [{ ...element, startTime: 0, muted: false }],
			} as TimelineTrack;
			const blob = await extractTimelineAudio({
				tracks: [renderTrack],
				mediaAssets,
				totalDuration: element.duration,
			});
			const base = sanitizeFilename({ name: element.name });
			const prefix = multiple
				? String(index + 1).padStart(2, "0") + "_"
				: "";
			downloadBlob({ blob, filename: prefix + base + ".wav" });
			if (multiple) await wait(300);
		}
		toast.success(
			multiple ? "已导出 " + items.length + " 段音频" : "音频已导出",
			{ id: toastId },
		);
	} catch (error) {
		console.error("Failed to export audio:", error);
		toast.error("导出音频失败", { id: toastId });
	}
}

/** 把选中的音频合并成一段（保留相对位置与空白间隔），方便整体导出 */
export async function mergeSelectedAudio({
	editor,
}: {
	editor: EditorCore;
}): Promise<void> {
	const items = collectSelectedAudio({ editor });
	if (items.length < 2) {
		toast.error("请选择至少两段音频再合并");
		return;
	}

	const trackId = items[0].track.id;
	if (items.some(({ track }) => track.id !== trackId)) {
		toast.error("只能合并在同一条音频轨道上的音频");
		return;
	}

	const project = editor.project.getActiveOrNull();
	if (!project) return;

	const start = Math.min(...items.map(({ element }) => element.startTime));
	const end = Math.max(
		...items.map(({ element }) => element.startTime + element.duration),
	);
	const duration = end - start;
	if (duration <= 0) {
		toast.error("音频时长无效");
		return;
	}

	const toastId = "merge-selected-audio";
	toast.loading("正在合并音频…", { id: toastId });

	try {
		const mediaAssets = editor.media.getAssets();
		const track = items[0].track;
		const renderTrack = {
			...track,
			muted: false,
			elements: items.map(({ element }) => ({
				...element,
				startTime: element.startTime - start,
				muted: false,
			})),
		} as TimelineTrack;
		const blob = await extractTimelineAudio({
			tracks: [renderTrack],
			mediaAssets,
			totalDuration: duration,
		});

		const name = "合并音频";
		const file = new File([blob], name + ".wav", { type: "audio/wav" });
		const url = URL.createObjectURL(blob);
		const mediaId = await editor.media.addMediaAsset({
			projectId: project.metadata.id,
			asset: { name, type: "audio", file, url, duration },
		});

		editor.timeline.deleteElements({
			elements: items.map(({ element }) => ({
				trackId,
				elementId: element.id,
			})),
		});

		// 删掉最后一段音频时，空音轨会被自动移除；这里重新取目标音频轨
		const remainingTracks = editor.timeline.getTracks();
		const targetTrack =
			remainingTracks.find((item) => item.id === trackId) ??
			remainingTracks.find((item) => item.type === "audio");
		if (!targetTrack) {
			toast.error("合并音频失败：找不到音频轨道", { id: toastId });
			return;
		}

		editor.timeline.insertElement({
			placement: { mode: "explicit", trackId: targetTrack.id },
			element: buildUploadAudioElement({
				mediaId,
				name,
				duration,
				startTime: start,
			}),
		});

		const mergedElement = editor.timeline
			.getTracks()
			.find((item) => item.id === targetTrack.id)
			?.elements.find(
				(element) => element.name === name && element.startTime === start,
			);
		if (!mergedElement) {
			toast.error("合并音频失败：目标位置被占用", { id: toastId });
			return;
		}

		// 选中合并后的片段，避免还停在已删除的旧选中项上
		editor.selection.setSelectedElements({
			elements: [{ trackId: targetTrack.id, elementId: mergedElement.id }],
		});

		toast.success("已合并为一段音频", { id: toastId });
	} catch (error) {
		console.error("Failed to merge audio:", error);
		toast.error("合并音频失败", { id: toastId });
	}
}
""",
)
print("  wrote apps/web/src/lib/audio-export.ts")

# 注册两个动作
p = SRC / "lib" / "actions" / "definitions.ts"
text = p.read_text(encoding="utf-8")
anchor = """	"export-selected-clip": {
		description: "Export selected clip",
		category: "editing",
	},
"""
addition = anchor + """	"export-selected-audio": {
		description: "Export selected audio",
		category: "editing",
	},
	"merge-selected-audio": {
		description: "Merge selected audio",
		category: "editing",
	},
"""
if anchor in text and "export-selected-audio" not in text:
    p.write_text(text.replace(anchor, addition, 1), encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)} (audio actions)")

# 动作处理器
p = SRC / "hooks" / "actions" / "use-editor-actions.ts"
text = p.read_text(encoding="utf-8")
text = text.replace(
    'import { getExportMimeType, getSelectedClipExportFilename } from "@/lib/export";',
    'import { getExportMimeType, getSelectedClipExportFilename } from "@/lib/export";\nimport { exportSelectedAudio, mergeSelectedAudio } from "@/lib/audio-export";',
    1,
)
anchor2 = """	useActionHandler(
		"freeze-frame",
"""
handlers = """	useActionHandler(
		"export-selected-audio",
		() => {
			void exportSelectedAudio({ editor });
		},
		undefined,
	);

	useActionHandler(
		"merge-selected-audio",
		() => {
			void mergeSelectedAudio({ editor });
		},
		undefined,
	);

""" + anchor2
if anchor2 in text and "export-selected-audio" not in text:
    p.write_text(text.replace(anchor2, handlers, 1), encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)} (audio action handlers)")

# 右键菜单：导出 / 合并
p = SRC / "components" / "editor" / "panels" / "timeline" / "timeline-element.tsx"
text = p.read_text(encoding="utf-8")
anchor3 = """				{element.type === "video" && selectedElements.length === 1 && (
					<ActionMenuItem
						action="export-selected-clip"
						icon={<Download className="size-4" />}
					>
						{t("Export selected clip")}
					</ActionMenuItem>
				)}
"""
menu = anchor3 + """				{element.type === "audio" && (
					<ContextMenuItem
						icon={<Download className="size-4" />}
						onClick={(event) => {
							event.stopPropagation();
							if (!isCurrentElementSelected) {
								editor.selection.setSelectedElements({
									elements: [{ trackId: track.id, elementId: element.id }],
								});
							}
							invokeAction("export-selected-audio");
						}}
					>
						{selectedElements.length > 1 ? "导出选中音频" : "导出音频"}
					</ContextMenuItem>
				)}
				{element.type === "audio" && selectedElements.length > 1 && (
					<ContextMenuItem
						icon={<HugeiconsIcon icon={MusicNote03Icon} />}
						onClick={(event) => {
							event.stopPropagation();
							invokeAction("merge-selected-audio");
						}}
					>
						合并选中的音频
					</ContextMenuItem>
				)}
"""
if anchor3 in text and "export-selected-audio" not in text:
    p.write_text(text.replace(anchor3, menu, 1), encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)} (audio context menu)")

# 老版本可能已经把 whisper-tiny / whisper-base 写进 localStorage，回退到默认模型
p = SRC / "components" / "editor" / "panels" / "assets" / "views" / "captions.tsx"
patch(
    p,
    '''\tconst [selectedModelId, setSelectedModelId] =
\t\tuseLocalStorage<TranscriptionModelId>({
\t\t\tkey: "editor-caption-model-id",
\t\t\tdefaultValue: DEFAULT_TRANSCRIPTION_MODEL,
\t\t});''',
    '''\tconst [storedModelId, setSelectedModelId] =
\t\tuseLocalStorage<TranscriptionModelId>({
\t\t\tkey: "editor-caption-model-id",
\t\t\tdefaultValue: DEFAULT_TRANSCRIPTION_MODEL,
\t\t});
\t// 已下线的模型 id（Tiny / Base）存在 localStorage 时回退到默认模型
\tconst selectedModelId: TranscriptionModelId = TRANSCRIPTION_MODELS.some(
\t\t(model) => model.id === storedModelId,
\t)
\t\t? storedModelId
\t\t: DEFAULT_TRANSCRIPTION_MODEL;''',
)

p = SRC / "services" / "transcription" / "service.ts"
patch(
    p,
    '\t\tconst model = TRANSCRIPTION_MODELS.find((m) => m.id === modelId);',
    '\t\tconst model =\n\t\t\tTRANSCRIPTION_MODELS.find((m) => m.id === modelId) ??\n\t\t\tTRANSCRIPTION_MODELS.find((m) => m.id === DEFAULT_TRANSCRIPTION_MODEL);',
)

# ------------------------------------------------ 弹层 -> 页面 的站内跳转桥
# 插件弹层里点「项目」时不再整页重载 iframe，而是让前端路由自己跳转。
write(
    SRC / "components" / "providers" / "host-bridge.tsx",
    """\"use client\";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

/** 监听 ComfyUI 弹层的消息，用前端路由做站内跳转（避免整页重载）。 */
export function HostBridge() {
\tconst router = useRouter();

\tuseEffect(() => {
\t\tconst onMessage = (event: MessageEvent) => {
\t\t\tconst data = event.data as { type?: string; path?: string } | null;
\t\t\tif (!data || data.type !== "yuancut:navigate" || !data.path) return;
\t\t\tif (!data.path.startsWith("/")) return;
\t\t\trouter.push(data.path);
\t\t\twindow.parent?.postMessage(
\t\t\t\t{ type: "yuancut:navigated", path: data.path },
\t\t\t\t"*",
\t\t\t);
\t\t};

\t\twindow.addEventListener("message", onMessage);
\t\treturn () => window.removeEventListener("message", onMessage);
\t}, [router]);

\treturn null;
}
""",
)

p = SRC / "app" / "[locale]" / "layout.tsx"
text = p.read_text(encoding="utf-8")
if "HostBridge" not in text:
    text = text.replace(
        'import { I18nProvider } from "@/components/providers/i18n-provider";',
        'import { I18nProvider } from "@/components/providers/i18n-provider";\nimport { HostBridge } from "@/components/providers/host-bridge";',
        1,
    )
    text = text.replace(
        "\t\t\t\t\t<TooltipProvider>",
        "\t\t\t\t\t<TooltipProvider>\n\t\t\t\t\t\t<HostBridge />",
        1,
    )
    p.write_text(text, encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)} (host bridge)")

# --------------------------------------------------------------- project links
p = SRC / "app" / "[locale]" / "projects" / "page.tsx"
text = p.read_text(encoding="utf-8")
if "/editor?project=" not in text:
    text = text.replace("router.push(`/editor/${projectId}`);", "router.push(`/editor?project=${projectId}`);")
    text = text.replace("href={`/editor/${project.id}`}", "href={`/editor?project=${project.id}`}")
    p.write_text(text, encoding="utf-8")
    print(f"  patched {p.relative_to(ROOT)}")

# --------------------------------------------------------- metadata routes
for name in ("robots.ts", "sitemap.ts"):
    p = SRC / "app" / name
    text = p.read_text(encoding="utf-8")
    if "force-static" not in text:
        text = text.replace(
            'import type { MetadataRoute } from "next";',
            'import type { MetadataRoute } from "next";\n\nexport const dynamic = "force-static";',
            1,
        )
        p.write_text(text, encoding="utf-8")
        print(f"  patched {p.relative_to(ROOT)}")

# ------------------------------------------------ 音频标签（音效 / 提取音频）
# 上游的 sounds 标签是 Freesound 在线搜索，静态导出后 /api/sounds 404，本来被隐藏。
# 这里把它换成离线可用的「音频」标签，位置正好在「媒体」与「文字」之间：
#   1) 上传音频 / 音效（走与「媒体」相同的素材导入管线）
#   2) 上传视频，用 mediabunny 抽出音轨并存成 WAV 素材
write(
    SRC / "components" / "editor" / "panels" / "assets" / "views" / "sounds.tsx",
    '''"use client";

import { useTranslation } from "@i18next-toolkit/nextjs-approuter";
import {
	CloudUploadIcon,
	MusicNote03Icon,
	Video01Icon,
} from "@hugeicons/core-free-icons";
import { HugeiconsIcon } from "@hugeicons/react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { SelectionBox } from "@/components/editor/selection-box";
import { GridView, MediaPreview } from "@/components/editor/panels/assets/views/media";
import { Button } from "@/components/ui/button";
import {
	ContextMenu,
	ContextMenuContent,
	ContextMenuItem,
	ContextMenuSeparator,
	ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { Progress } from "@/components/ui/progress";
import {
	Tooltip,
	TooltipContent,
	TooltipProvider,
	TooltipTrigger,
} from "@/components/ui/tooltip";
import { useAssetMarquee } from "@/hooks/use-asset-marquee";
import { useEditor } from "@/hooks/use-editor";
import { useFileUpload } from "@/hooks/use-file-upload";
import { extractSourceAudio, getVideoInfo } from "@/lib/media/mediabunny";
import { processMediaAssets } from "@/lib/media/processing";
import { buildUploadAudioElement } from "@/lib/timeline/element-utils";
import { useMediaPreviewStore } from "@/stores/media-preview-store";
import type { MediaAsset } from "@/types/assets";

const AUDIO_ONLY = "audio/*";
const VIDEO_ONLY = "video/*";
const ANY_MEDIA = "audio/*,video/*";

function stripExtension({ name }: { name: string }): string {
	const index = name.lastIndexOf(".");
	return index > 0 ? name.slice(0, index) : name;
}

export function SoundsView() {
	const { t } = useTranslation();
	const editor = useEditor();
	const project = editor.project.getActive();
	// 音频面板只显示 panel=audio 的素材；卡片样式和媒体面板完全共用
	const audioAssets = editor.media
		.getAssets()
		.filter((asset) => asset.panel === "audio" && !asset.ephemeral);

	const panelRef = useRef<HTMLDivElement>(null);
	const audioInputRef = useRef<HTMLInputElement>(null);
	const videoInputRef = useRef<HTMLInputElement>(null);
	const marquee = useAssetMarquee({ containerRef: panelRef });
	const selectedMediaId = useMediaPreviewStore((state) => state.selectedMediaId);

	const [busy, setBusy] = useState(false);
	const [status, setStatus] = useState("");
	const [progress, setProgress] = useState(0);

	const openAudioPicker = () => audioInputRef.current?.click();
	const openVideoPicker = () => videoInputRef.current?.click();

	const addProcessedFiles = async ({ files }: { files: File[] }) => {
		if (!project) return 0;
		const processed = await processMediaAssets({
			files,
			onProgress: ({ progress: value }) => setProgress(value),
		});
		for (const asset of processed) {
			await editor.media.addMediaAsset({
				projectId: project.metadata.id,
				// 音频面板导入/提取的素材固定归到 audio，不会出现在媒体面板
				asset: { ...asset, panel: "audio" },
			});
		}
		return processed.length;
	};

	const importAudioFiles = async ({ files }: { files: File[] }) => {
		if (files.length === 0) return;
		if (!project) {
			toast.error(t("No active project"));
			return;
		}
		setBusy(true);
		setStatus(t("正在导入音频…"));
		setProgress(0);
		try {
			const count = await addProcessedFiles({ files });
			if (count > 0) {
				toast.success(t("已导入 {{count}} 个音频", { count }));
			}
		} catch (error) {
			console.error("Audio import failed:", error);
			toast.error(t("音频导入失败"));
		} finally {
			setBusy(false);
			setStatus("");
			setProgress(0);
		}
	};

	const extractFromVideos = async ({ files }: { files: File[] }) => {
		if (files.length === 0) return;
		if (!project) {
			toast.error(t("No active project"));
			return;
		}
		setBusy(true);
		setProgress(0);
		const extractedFiles: File[] = [];
		try {
			for (let index = 0; index < files.length; index++) {
				const file = files[index];
				setStatus(
					t("正在提取音频 {{current}}/{{total}}", {
						current: index + 1,
						total: files.length,
					}),
				);
				try {
					const info = await getVideoInfo({ videoFile: file });
					const blob = await extractSourceAudio({
						source: {
							file,
							startTime: 0,
							duration: info.duration,
							trimStart: 0,
							playbackRate: 1,
						},
					});
					extractedFiles.push(
						new File([blob], stripExtension({ name: file.name }) + ".wav", {
							type: "audio/wav",
						}),
					);
				} catch (error) {
					console.error("Audio extraction failed:", file.name, error);
					toast.error(t("音频提取失败：{{name}}", { name: file.name }));
				}
				setProgress(Math.round(((index + 1) / files.length) * 100));
			}
			if (extractedFiles.length > 0) {
				const count = await addProcessedFiles({ files: extractedFiles });
				if (count > 0) {
					toast.success(t("已提取 {{count}} 个音频", { count }));
				}
			}
		} catch (error) {
			console.error("Audio extraction failed:", error);
			toast.error(t("音频提取失败"));
		} finally {
			setBusy(false);
			setStatus("");
			setProgress(0);
		}
	};

	const handleFiles = ({ files }: { files: File[] }) => {
		const videos = files.filter((file) => file.type.startsWith("video/"));
		const audios = files.filter((file) => file.type.startsWith("audio/"));
		if (videos.length > 0) void extractFromVideos({ files: videos });
		if (audios.length > 0) void importAudioFiles({ files: audios });
	};

	// 和媒体面板同一套：点卡片 = 选中，播放/进度条在右侧预览框里
	const handleSelect = ({ asset }: { asset: MediaAsset }) => {
		const store = useMediaPreviewStore.getState();
		if (store.selectedMediaId === asset.id) {
			store.clearSelection();
		} else {
			store.selectMedia({ mediaId: asset.id });
		}
	};

	const handleRemove = ({
		event,
		id,
	}: {
		event: React.MouseEvent;
		id: string;
	}) => {
		event.stopPropagation();
		if (!project) return;
		void editor.media.removeMediaAsset({ projectId: project.metadata.id, id });
	};

	const handleExportClip = ({ item }: { item: MediaAsset }) => {
		const downloadUrl = URL.createObjectURL(item.file);
		const link = document.createElement("a");
		link.href = downloadUrl;
		link.download = item.file.name || item.name;
		document.body.append(link);
		link.click();
		link.remove();
		setTimeout(() => URL.revokeObjectURL(downloadUrl), 0);
	};

	const addElementAtTime = ({
		asset,
		startTime,
	}: {
		asset: MediaAsset;
		startTime: number;
	}): boolean => {
		editor.timeline.insertElement({
			element: buildUploadAudioElement({
				mediaId: asset.id,
				name: asset.name,
				duration: asset.duration ?? 0,
				startTime,
			}),
			placement: { mode: "auto" },
		});
		return true;
	};

	const handleRemoveSelected = async () => {
		if (!project || marquee.selectedIds.length === 0) return;
		const ids = [...marquee.selectedIds];
		marquee.clearSelection();
		for (const id of ids) {
			await editor.media.removeMediaAsset({ projectId: project.metadata.id, id });
		}
	};

	// 键盘 Delete / Backspace 删除选中的素材
	useEffect(() => {
		if (marquee.selectedIds.length === 0) return;
		const onKeyDown = (event: KeyboardEvent) => {
			if (event.key !== "Delete" && event.key !== "Backspace") return;
			const target = event.target as HTMLElement | null;
			if (
				target &&
				(target.tagName === "INPUT" ||
					target.tagName === "TEXTAREA" ||
					target.isContentEditable)
			) {
				return;
			}
			event.preventDefault();
			void handleRemoveSelected();
		};
		window.addEventListener("keydown", onKeyDown);
		return () => window.removeEventListener("keydown", onKeyDown);
	});

	const { isDragOver, dragProps, fileInputProps } = useFileUpload({
		accept: ANY_MEDIA,
		multiple: true,
		onFilesSelected: (files) => handleFiles({ files: Array.from(files) }),
	});

	const selectedCount = marquee.selectedIds.length;

	return (
		<div className="relative flex h-full flex-col gap-1" {...dragProps}>
			<input {...fileInputProps} />
			<input
				ref={audioInputRef}
				type="file"
				accept={AUDIO_ONLY}
				multiple
				hidden
				onChange={(event) => {
					const files = event.target.files
						? Array.from(event.target.files)
						: [];
					event.target.value = "";
					void importAudioFiles({ files });
				}}
			/>
			<input
				ref={videoInputRef}
				type="file"
				accept={VIDEO_ONLY}
				multiple
				hidden
				onChange={(event) => {
					const files = event.target.files
						? Array.from(event.target.files)
						: [];
					event.target.value = "";
					void extractFromVideos({ files });
				}}
			/>

			<ContextMenu>
				<ContextMenuTrigger asChild>
					<div
						ref={panelRef}
						className={`relative flex h-full flex-col gap-1 ${isDragOver ? "bg-accent/30" : ""}`}
					>
						<SelectionBox
							startPos={marquee.startPos}
							currentPos={marquee.currentPos}
							containerRef={panelRef}
							isActive={marquee.active}
						/>

						<div className="bg-background flex h-12 items-center justify-between border-b px-4 pr-2">
							<span className="text-muted-foreground text-sm">{t("音频")}</span>
							<TooltipProvider>
								<div className="flex items-center gap-0">
									<Tooltip delayDuration={10}>
										<TooltipTrigger asChild>
											<Button
												size="icon"
												variant="text"
												disabled={busy}
												onClick={openAudioPicker}
											>
												<HugeiconsIcon icon={MusicNote03Icon} />
											</Button>
										</TooltipTrigger>
										<TooltipContent>{t("添加音频 / 音效")}</TooltipContent>
									</Tooltip>
									<Tooltip delayDuration={10}>
										<TooltipTrigger asChild>
											<Button
												size="icon"
												variant="text"
												disabled={busy}
												onClick={openVideoPicker}
											>
												<HugeiconsIcon icon={Video01Icon} />
											</Button>
										</TooltipTrigger>
										<TooltipContent>{t("从视频提取音频")}</TooltipContent>
									</Tooltip>
								</div>
							</TooltipProvider>
						</div>

						{busy && (
							<div className="flex flex-col gap-1 border-b px-4 py-2">
								<span className="text-muted-foreground text-xs">{status}</span>
								<Progress value={progress} />
							</div>
						)}

						<div
							className="scrollbar-thin size-full overflow-y-auto"
							onMouseDown={marquee.handleMouseDown}
						>
							<div className="w-full flex-1 p-2 pt-1">
								{audioAssets.length === 0 ? (
									<button
										type="button"
										onClick={openAudioPicker}
										className="border-muted-foreground/30 text-muted-foreground flex h-40 w-full flex-col items-center justify-center gap-2 rounded-sm border border-dashed text-xs"
									>
										<HugeiconsIcon icon={CloudUploadIcon} className="size-8" />
										<span>{t("点这里添加音频或音效")}</span>
										<span className="text-[10px]">
											{t("也可以把音频或视频拖进来；视频会自动转成 WAV 音轨")}
										</span>
									</button>
								) : (
									<GridView
										items={audioAssets}
										renderPreview={(item) => <MediaPreview item={item} />}
										onRemove={handleRemove}
										onExportClip={handleExportClip}
										onAddToTimeline={addElementAtTime}
										onSelect={handleSelect}
										selectedMediaId={selectedMediaId}
										highlightedId={null}
										registerElement={() => {}}
									/>
								)}
							</div>
						</div>

						{selectedCount > 0 && (
							<div className="bg-foreground/85 text-background pointer-events-none absolute right-3 bottom-3 z-40 rounded px-2 py-1 text-[11px] shadow">
								已选 {selectedCount} 项
							</div>
						)}
					</div>
				</ContextMenuTrigger>
				<ContextMenuContent>
					<ContextMenuItem
						variant="destructive"
						disabled={selectedCount === 0}
						onClick={() => void handleRemoveSelected()}
					>
						删除
					</ContextMenuItem>
					<ContextMenuSeparator />
					<ContextMenuItem onClick={() => marquee.clearSelection()}>
						取消选择
					</ContextMenuItem>
				</ContextMenuContent>
			</ContextMenu>
		</div>
	);
}
''',
)
print("  wrote   sounds.tsx（音频 / 音效 / 视频提取音频）")

# 标签留在「媒体」与「文字」之间，只把文案换成「音频」
p = SRC / "stores" / "assets-panel-store.tsx"
patch(p, '\tsounds: "Sounds",', '\tsounds: "音频",')

 
# The plugin opens on "All projects", so the landing page, the marketing routes
# and their chrome/assets are dead weight (and full of links to a removed home).
for rel in (
    "src/app/[locale]/page.tsx",
    "src/app/[locale]/playground",
    "src/app/[locale]/privacy",
    "src/app/[locale]/terms",
    "src/app/[locale]/roadmap",
    "src/app/[locale]/why-not-capcut",
    "src/app/base-page.tsx",
    "src/components/landing",
    "src/components/header.tsx",
    "src/components/footer.tsx",
    "src/components/gitHub-contribute-section.tsx",
    "src/app/robots.ts",
    "src/app/sitemap.ts",
    "public/preview",
    "public/landing-page-dark.png",
    "public/llms.txt",
):
    target = WEB / rel
    if target.is_dir():
        shutil.rmtree(target)
        print(f"  removed {rel}/")
    elif target.exists():
        target.unlink()
        print(f"  removed {rel}")

# Both remaining pages had a "Home / ..." breadcrumb leading to the removed
# landing page. Drop that single crumb and keep the rest of the trail.
home_crumb = re.compile(
    r"[ \t]*<BreadcrumbItem>\s*"
    r"<BreadcrumbLink asChild>\s*"
    r'<Link\s+href="/"[^>]*>\s*'
    r'\{t\("Home"\)\}\s*'
    r"</Link>\s*"
    r"</BreadcrumbLink>\s*"
    r"</BreadcrumbItem>\s*"
    r"<BreadcrumbSeparator />[ \t]*\n"
)
for rel in (
    "src/app/[locale]/projects/page.tsx",
    "src/app/[locale]/characters/page.tsx",
):
    p = SRC / "app" / rel.split("src/app/", 1)[1]
    text = p.read_text(encoding="utf-8")
    text2, count = home_crumb.subn("", text, count=1)
    if count:
        p.write_text(text2, encoding="utf-8")
        print(f"  patched {p.relative_to(ROOT)} (drop Home breadcrumb)")

# ---------------------------------------------------------------- rebrand
# 插件里显示的品牌统一为 YuanCut：大写的品牌词、存储键、站点 URL、社交账号
# 都换掉；只有上游 LICENSE 的版权声明保持原样（MIT 要求保留）。
LEGACY_SUBSTRINGS = {
    "cutia.msgbyte.com": "yuancut.local",
    "@cutiaapp": "@yuancut",
    "github.com/msgbyte/cutia": "github.com/msgbyte",
    "cutia-keybindings": "yuancut-keybindings",
}

rebranded = 0
for path in sorted(SRC.rglob("*")):
    if not path.is_file() or path.suffix not in {".ts", ".tsx", ".css", ".json"}:
        continue
    text = path.read_text(encoding="utf-8")
    branded = text.replace("Cutia", "YuanCut")
    for old, new in LEGACY_SUBSTRINGS.items():
        branded = branded.replace(old, new)
    if branded == text:
        continue
    path.write_text(branded, encoding="utf-8")
    rebranded += 1
print(f"  rebranded {rebranded} source file(s) to YuanCut")

logo_dir = WEB / "public" / "logos"
if (logo_dir / "cutia").is_dir() and not (logo_dir / "yuancut").exists():
    (logo_dir / "cutia").rename(logo_dir / "yuancut")
    print("  renamed public/logos/cutia -> public/logos/yuancut")

for path in [*SRC.rglob("*.ts"), *SRC.rglob("*.tsx")]:
    text = path.read_text(encoding="utf-8")
    if "/logos/cutia/" in text:
        path.write_text(text.replace("/logos/cutia/", "/logos/yuancut/"), encoding="utf-8")

for rel in ("public/manifest.json", "public/llms.txt"):
    p = WEB / rel
    if not p.is_file():
        continue
    text = p.read_text(encoding="utf-8")
    text = text.replace("Cutia", "YuanCut").replace("/logos/cutia/", "/logos/yuancut/")
    p.write_text(text, encoding="utf-8")
    print(f"  rebranded {p.relative_to(ROOT)}")

for p in sorted((WEB / "public" / "locales").rglob("*.json")):
    text = p.read_text(encoding="utf-8")
    branded = text.replace("Cutia", "YuanCut")
    for old, new in LEGACY_SUBSTRINGS.items():
        branded = branded.replace(old, new)
    if branded == text:
        continue
    p.write_text(branded, encoding="utf-8")

# ------------------------------------------------ 品牌图标（只保留必要的两份）
# logo.svg：界面 logo、浏览器标签图标、OG 图（矢量，一份通用）
# favicon.ico：老式标签/书签兜底（一个文件内含 16/32/48/64）
# 其余 icon.svg 副本与 PNG 尺寸、manifest 里的 PNG 图标都不再生成。
ASSETS = Path(__file__).resolve().parent.parent / "assets"
if (ASSETS / "logo.svg").is_file():
    logo_target = WEB / "public" / "logos" / "yuancut" / "svg" / "logo.svg"
    logo_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ASSETS / "logo.svg", logo_target)

    # Next 的 app/icon.svg 会再产出一份 /icon.svg 副本，直接删掉
    app_icon = WEB / "src" / "app" / "icon.svg"
    if app_icon.exists():
        app_icon.unlink()

    favicon = ASSETS / "favicon.ico"
    if favicon.is_file():
        shutil.copyfile(favicon, WEB / "public" / "favicon.ico")

    # 历史的多尺寸 PNG（android/apple/ms/favicon）全部删除
    icons_dir = WEB / "public" / "icons"
    if icons_dir.is_dir():
        shutil.rmtree(icons_dir)

    # 官网遗留的合作方 logo（着陆页已删除，不再引用）
    others = WEB / "public" / "logos" / "others"
    if others.is_dir():
        shutil.rmtree(others)

    # 界面与 OG 图统一指向 logo.svg
    p = SRC / "constants" / "site-constants.ts"
    text = p.read_text(encoding="utf-8")
    text = text.replace(
        'openGraphImage: "/icon.svg",',
        'openGraphImage: withBasePath("/logos/yuancut/svg/logo.svg"),',
        1,
    )
    text = text.replace(
        'twitterImage: "/icon.svg",',
        'twitterImage: withBasePath("/logos/yuancut/svg/logo.svg"),',
        1,
    )
    p.write_text(text, encoding="utf-8")

    manifest_path = WEB / "public" / "manifest.json"
    if manifest_path.is_file():
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["icons"] = [
            {
                "src": "/yuancut/logos/yuancut/svg/logo.svg",
                "sizes": "any",
                "type": "image/svg+xml",
            }
        ]
        manifest_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print("  installed logo.svg + favicon.ico (only two icon files)")

# ============================================================================
# 导出健壮性（A）与取帧提速（C）
#   1. 取帧诊断：导出期间统计每个片段的「请求帧/缺帧」，整段取不到画面时
#      直接报错中止，而不是静默产出一个黑屏视频。
#   2. 导出前预检：每个视频素材先解第 0 秒一帧，解不出来就提前报错并点名素材。
#   3. 提速：取帧缓存按「素材 + 片段」隔离（cacheKey），避免同一素材被多个
#      片段交替请求时反复整段 seek —— 这是长视频导出慢的主要来源。
# ============================================================================
print("==> 导出健壮性 + 取帧提速")

TAB = chr(9)
NL = chr(10)


def tabsify(block: str) -> str:
    """把用 2 空格缩进写的 TS 代码块转成 tab 缩进（上游代码用 tab）。"""
    out = []
    for raw in block.strip("\n").split("\n"):
        stripped = raw.lstrip(" ")
        depth = (len(raw) - len(stripped)) // 2
        out.append(TAB * depth + stripped)
    return "\n".join(out)


def sub_src(path: Path, pattern: str, repl, *, marker: str, expect: int = 1) -> None:
    """缩进容错的正则补丁；repl 用函数避免转义歧义。marker 已存在则跳过。"""
    text = path.read_text(encoding="utf-8")
    if marker in text:
        print(f"  keep    {path.relative_to(ROOT)}")
        return
    new_text, count = re.subn(pattern, repl, text, flags=re.S | re.M)
    if count == 0:
        raise SystemExit(f"anchor not found in {path.relative_to(ROOT)}: {pattern[:160]}")
    if count != expect:
        raise SystemExit(
            f"{path.relative_to(ROOT)}: expected {expect} match(es), got {count}"
        )
    path.write_text(new_text, encoding="utf-8")
    print(f"  patched {path.relative_to(ROOT)}")


def indent_block(block: str, levels: int) -> str:
    prefix = TAB * levels
    return "\n".join(
        prefix + line if line.strip() else line for line in block.split("\n")
    )


def replace_between(path: Path, start: str, end: str, body: str, *, marker: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        print(f"  keep    {path.relative_to(ROOT)}")
        return
    i = text.find(start)
    if i < 0:
        raise SystemExit(f"start anchor not found in {path.relative_to(ROOT)}: {start}")
    j = text.find(end, i)
    if j < 0:
        raise SystemExit(f"end anchor not found in {path.relative_to(ROOT)}: {end}")
    normalized = body.rstrip("\n") + "\n\n"
    path.write_text(text[:i] + normalized + text[j:], encoding="utf-8")
    print(f"  patched {path.relative_to(ROOT)}")


# ---------------------------------------------------------------- video-cache
VIDEO_CACHE = SRC / "services" / "video-cache" / "service.ts"

FRAME_REQUIREMENT_INTERFACE = tabsify("""
export interface FrameRequirement {
  cacheKey: string;
  mediaId: string;
  assetName: string;
  requested: number;
  missing: number;
  firstMissingTime: number | null;
  lastReason: string | null;
  assetMissing: boolean;
}

export interface PreflightResult {
  ok: boolean;
  failures: { name: string; reason: string }[];
}

""")

sub_src(
    VIDEO_CACHE,
    r"^export class VideoCache \{",
    lambda m: FRAME_REQUIREMENT_INTERFACE + "\n" + m.group(0),
    marker="export interface FrameRequirement {",
)

DIAGNOSTICS_MEMBERS = tabsify("""

  private diagnosticsArmed = false;
  private frameRequirements = new Map<string, FrameRequirement>();

  /** 导出开始时调用：清空并开始记录取帧情况。 */
  beginDiagnostics(): void {
    this.diagnosticsArmed = true;
    this.frameRequirements.clear();
  }

  /** 导出结束时调用：返回本轮记录（会清空并停止记录）。 */
  endDiagnostics(): FrameRequirement[] {
    this.diagnosticsArmed = false;
    const records = [...this.frameRequirements.values()];
    this.frameRequirements.clear();
    return records;
  }

  /** 导出前预检：每个视频素材先解第 0 秒一帧，解不出就别开始导出（否则成品是黑屏）。 */
  async preflight({
    assets,
  }: {
    assets: { mediaId: string; file: File }[];
  }): Promise<PreflightResult> {
    const failures: { name: string; reason: string }[] = [];

    for (const asset of assets) {
      const cacheKey = `${asset.mediaId}:preflight`;
      try {
        const frame = await this.getFrameAt({
          mediaId: asset.mediaId,
          file: asset.file,
          time: 0,
          cacheKey,
        });
        if (!frame) {
          failures.push({
            name: asset.file.name,
            reason: "第 0 秒解不出画面（编码不受支持或文件损坏）",
          });
        }
      } catch (error) {
        failures.push({
          name: asset.file.name,
          reason: error instanceof Error ? error.message : String(error),
        });
      } finally {
        this.clearVideo({ mediaId: cacheKey });
      }
    }

    return { ok: failures.length === 0, failures };
  }

  private noteFrame({
    cacheKey,
    mediaId,
    file,
    time,
    hadFrame,
    reason,
  }: {
    cacheKey: string;
    mediaId: string;
    file: File;
    time: number;
    hadFrame: boolean;
    reason?: string;
  }): void {
    if (!this.diagnosticsArmed) return;

    let record = this.frameRequirements.get(cacheKey);
    if (!record) {
      record = {
        cacheKey,
        mediaId,
        assetName: file.name,
        requested: 0,
        missing: 0,
        firstMissingTime: null,
        lastReason: null,
        assetMissing: false,
      };
      this.frameRequirements.set(cacheKey, record);
    }

    record.requested += 1;
    if (hadFrame) return;

    record.missing += 1;
    if (record.firstMissingTime === null) record.firstMissingTime = time;
    if (reason) record.lastReason = reason;
  }

  /** buildScene 跳过「没有 file/url」的素材时记一笔，导出结束可以说清是哪一条。 */
  recordMissingAsset({
    elementId,
    mediaId,
  }: {
    elementId: string;
    mediaId: string;
  }): void {
    if (!this.diagnosticsArmed) return;

    const cacheKey = `missing-asset:${elementId}`;
    if (this.frameRequirements.has(cacheKey)) return;

    this.frameRequirements.set(cacheKey, {
      cacheKey,
      mediaId,
      assetName: mediaId,
      requested: 0,
      missing: 0,
      firstMissingTime: null,
      lastReason: "media-file-missing",
      assetMissing: true,
    });
  }
""")

sub_src(
    VIDEO_CACHE,
    r"^([ \t]*)private initPromises = new Map<string, Promise<void>>\(\);",
    lambda m: m.group(0) + "\n" + DIAGNOSTICS_MEMBERS,
    marker="private diagnosticsArmed = false;",
)

GET_FRAME_AT_HEAD = tabsify("""
async getFrameAt(args: {
  mediaId: string;
  file: File;
  time: number;
  cacheKey?: string;
}): Promise<WrappedCanvas | null> {
  const key = args.cacheKey ?? args.mediaId;

  // 超时兜底：解码或预取卡住时不要让预览/导出无限等待（卡在某个百分比）
  const frame = await Promise.race([
    this.resolveFrame({
      mediaId: args.mediaId,
      file: args.file,
      time: args.time,
      key,
    }),
    new Promise<null>((resolve) => {
      setTimeout(() => resolve(null), 5000);
    }),
  ]);

  this.noteFrame({
    cacheKey: key,
    mediaId: args.mediaId,
    file: args.file,
    time: args.time,
    hadFrame: frame !== null,
    reason: frame ? undefined : "no-frame-decoded",
  });

  if (!frame) {
    // 清掉卡住的预取状态，下一帧重新来，避免一直等同一个 Promise
    const stalled = this.sinks.get(key);
    if (stalled) {
      stalled.prefetching = false;
      stalled.prefetchPromise = null;
      stalled.iterator = null;
    }
  }

  return frame;
}

private async resolveFrame({
  mediaId,
  file,
  time,
  key,
}: {
  mediaId: string;
  file: File;
  time: number;
  key: string;
}): Promise<WrappedCanvas | null> {
  await this.ensureSink({ key, mediaId, file });

  const sinkData = this.sinks.get(key);
""")

sub_src(
    VIDEO_CACHE,
    r"async getFrameAt\(\{\s*\n[ \t]*mediaId,\n[ \t]*file,\n[ \t]*time,\n[ \t]*\}\s*:\s*\{\n"
    r"[ \t]*mediaId: string;\n[ \t]*file: File;\n[ \t]*time: number;\n[ \t]*\}\): Promise<WrappedCanvas \| null> \{\n"
    r"[ \t]*await this\.ensureSink\(\{ mediaId, file \}\);\n\s*"
    r"([ \t]*)const sinkData = this\.sinks\.get\(mediaId\);",
    lambda m: GET_FRAME_AT_HEAD,
    marker="private async resolveFrame({",
)

NEW_SINKS = tabsify("""
private async ensureSink({
  key,
  mediaId,
  file,
}: {
  key: string;
  mediaId: string;
  file: File;
}): Promise<void> {
  if (this.sinks.has(key)) return;

  if (this.initPromises.has(key)) {
    await this.initPromises.get(key);
    return;
  }

  const initPromise = this.initializeSink({ key, mediaId, file });
  this.initPromises.set(key, initPromise);

  try {
    await initPromise;
  } finally {
    this.initPromises.delete(key);
  }
}

private async initializeSink({
  key,
  mediaId,
  file,
}: {
  key: string;
  mediaId: string;
  file: File;
}): Promise<void> {
  try {
    const input = new Input({
      source: new BlobSource(file),
      formats: ALL_FORMATS,
    });

    const videoTrack = await input.getPrimaryVideoTrack();
    if (!videoTrack) {
      throw new Error("No video track found");
    }

    const canDecode = await videoTrack.canDecode();
    if (!canDecode) {
      throw new Error("Video codec not supported for decoding");
    }

    const sink = new CanvasSink(videoTrack, {
      poolSize: 3,
      fit: "contain",
    });

    this.sinks.set(key, {
      sink,
      iterator: null,
      currentFrame: null,
      nextFrame: null,
      lastTime: -1,
      prefetching: false,
      prefetchPromise: null,
    });
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    console.error(
      `Failed to initialize video sink for ${mediaId} (${file.name}):`,
      error,
    );
    throw new Error(
      `无法解码视频素材「${file.name}」：${reason}。请改用 H.264(MP4) 或 VP9(WebM) 编码的素材后重试。`,
    );
  }
}

""")

replace_between(
    VIDEO_CACHE,
    TAB + "private async ensureSink({",
    TAB + "clearVideo({ mediaId }: { mediaId: string }): void {",
    NEW_SINKS,
    marker="private async ensureSink({\n" + TAB + "key,",
)

NEW_CLEAR_VIDEO = tabsify("""
clearVideo({ mediaId }: { mediaId: string }): void {
  // mediaId 既可能是裸素材 id，也可能是「素材:片段」形式的 cacheKey，
  // 两种都清掉，避免素材删除后 sink 泄漏。
  const keys = [
    mediaId,
    ...[...this.sinks.keys()].filter((key) => key.startsWith(`${mediaId}:`)),
    ...[...this.initPromises.keys()].filter((key) =>
      key.startsWith(`${mediaId}:`),
    ),
  ];

  for (const key of keys) {
    const sinkData = this.sinks.get(key);
    if (sinkData) {
      if (sinkData.iterator) {
        void sinkData.iterator.return();
      }
      this.sinks.delete(key);
    }

    this.initPromises.delete(key);
  }
}

""")

replace_between(
    VIDEO_CACHE,
    TAB + "clearVideo({ mediaId }: { mediaId: string }): void {",
    TAB + "clearAll(): void {",
    NEW_CLEAR_VIDEO,
    marker="key.startsWith(`${mediaId}:`)",
)

# ---------------------------------------------------------------- video-node
VIDEO_NODE = SRC / "services" / "renderer" / "nodes" / "video-node.ts"

sub_src(
    VIDEO_NODE,
    r"^([ \t]*)mediaId: string;",
    lambda m: m.group(0) + "\n" + m.group(1) + "cacheKey?: string;",
    marker="cacheKey?: string;",
)

sub_src(
    VIDEO_NODE,
    r"([ \t]*)const frame = await videoCache\.getFrameAt\(\{\n"
    r"[ \t]*mediaId: this\.params\.mediaId,\n"
    r"[ \t]*file: this\.params\.file,\n"
    r"[ \t]*time: videoTime,\n"
    r"([ \t]*)\}\);",
    lambda m: (
        m.group(1)
        + "const frame = await videoCache.getFrameAt({\n"
        + m.group(1)
        + TAB
        + "mediaId: this.params.mediaId,\n"
        + m.group(1)
        + TAB
        + "file: this.params.file,\n"
        + m.group(1)
        + TAB
        + "time: videoTime,\n"
        + m.group(1)
        + TAB
        + "cacheKey: this.params.cacheKey,\n"
        + m.group(1)
        + "});"
    ),
    marker="cacheKey: this.params.cacheKey,",
)

# -------------------------------------------------------------- scene-builder
SCENE_BUILDER = SRC / "services" / "renderer" / "scene-builder.ts"

sub_src(
    SCENE_BUILDER,
    r'^(import type \{ BaseNode \} from "\./nodes/base-node";)$',
    lambda m: m.group(0)
    + '\nimport { videoCache } from "@/services/video-cache/service";',
    marker='import { videoCache } from "@/services/video-cache/service";',
)

sub_src(
    SCENE_BUILDER,
    r"([ \t]*)const mediaAsset = mediaMap\.get\(element\.mediaId\);\n"
    r"([ \t]*)if \(!mediaAsset\?\.file \|\| !mediaAsset\?\.url\) \{\n"
    r"[ \t]*return null;",
    lambda m: (
        m.group(1)
        + "const mediaAsset = mediaMap.get(element.mediaId);\n"
        + m.group(2)
        + "if (!mediaAsset?.file || !mediaAsset?.url) {\n"
        + m.group(2)
        + TAB
        + "videoCache.recordMissingAsset({\n"
        + m.group(2)
        + TAB * 2
        + "elementId: element.id,\n"
        + m.group(2)
        + TAB * 2
        + "mediaId: element.mediaId,\n"
        + m.group(2)
        + TAB
        + "});\n"
        + m.group(2)
        + TAB
        + "return null;"
    ),
    marker="videoCache.recordMissingAsset({",
)

sub_src(
    SCENE_BUILDER,
    r"^([ \t]*)mediaId: mediaAsset\.id,",
    lambda m: m.group(0) + "\n" + m.group(1) + "cacheKey: element.id,",
    marker="cacheKey: element.id,",
)

# ------------------------------------------------------------- scene-exporter
SCENE_EXPORTER = SRC / "services" / "renderer" / "scene-exporter.ts"

sub_src(
    SCENE_EXPORTER,
    r'^(import \{ CanvasRenderer \} from "\./canvas-renderer";)$',
    lambda m: m.group(0)
    + '\nimport { videoCache } from "@/services/video-cache/service";',
    marker='import { videoCache } from "@/services/video-cache/service";',
)

# 诊断由 renderer-manager 在 buildScene 前开启：素材缺失是在 buildScene 里记的，
# 必须比 SceneExporter.export() 更早，否则会漏掉。

sub_src(
    SCENE_EXPORTER,
    r"([ \t]*)if \(this\.isCancelled\) \{\n"
    r"[ \t]*await output\.cancel\(\);\n"
    r"[ \t]*this\.emit\(\"cancelled\"\);\n"
    r"[ \t]*return null;",
    lambda m: (
        m.group(1)
        + 'if (this.isCancelled) {\n'
        + m.group(1)
        + TAB
        + "videoCache.endDiagnostics();\n"
        + m.group(1)
        + TAB
        + "await output.cancel();\n"
        + m.group(1)
        + TAB
        + 'this.emit("cancelled");\n'
        + m.group(1)
        + TAB
        + "return null;"
    ),
    marker="videoCache.endDiagnostics();\n" + TAB * 2 + TAB + "await output.cancel();",
    expect=2,
)

EXPORT_FRAME_REPORT = tabsify("""
const requirements = videoCache.endDiagnostics();
const deadClips = requirements.filter(
  (record) =>
    record.assetMissing ||
    (record.requested > 0 && record.missing === record.requested),
);

if (deadClips.length > 0) {
  const detail = deadClips
    .slice(0, 5)
    .map((record) =>
      record.assetMissing
        ? `· ${record.mediaId}：素材缺少文件或地址，整段画面被跳过`
        : `· ${record.assetName}：${record.requested} 帧全部取不到画面（${record.lastReason ?? "解码失败"}）`,
    )
    .join("\\n");

  const error = new Error(
    `导出中止：有 ${deadClips.length} 个片段从头到尾没有画面，成品只会是黑屏。\\n${detail}\\n请把素材转成 H.264（MP4）或 VP9（WebM）后重试。`,
  );
  await output.cancel();
  this.emit("error", error);
  throw error;
}

const partialMisses = requirements.filter(
  (record) => record.missing > 0 && !deadClips.includes(record),
);
if (partialMisses.length > 0) {
  console.warn(
    "[YuanCut] 部分帧取不到画面（已用黑帧填充）：",
    partialMisses.map(
      (record) => `${record.assetName} ${record.missing}/${record.requested} 帧`,
    ),
  );
}

""")

sub_src(
    SCENE_EXPORTER,
    r"^([ \t]*)videoSource\.close\(\);",
    lambda m: indent_block(EXPORT_FRAME_REPORT, 2) + "\n" + m.group(0),
    marker="videoCache.endDiagnostics();\n\nconst deadClips",
)

# --------------------------------------------------------- renderer-manager
RENDERER_MANAGER = SRC / "core" / "managers" / "renderer-manager.ts"

sub_src(
    RENDERER_MANAGER,
    r'^import type \{ TimelineTrack \} from "@/types/timeline";$',
    lambda m: 'import type { TimelineTrack, VideoElement } from "@/types/timeline";\n'
    'import { videoCache } from "@/services/video-cache/service";',
    marker='import { videoCache } from "@/services/video-cache/service";',
)

PREFLIGHT_BLOCK = tabsify("""

const videoAssets = new Map<string, { mediaId: string; file: File }>();
for (const track of tracks) {
  for (const element of track.elements) {
    if (element.type !== "video") continue;
    const videoElement = element as VideoElement;
    if (videoElement.hidden) continue;

    const asset = mediaAssets.find((item) => item.id === videoElement.mediaId);
    if (asset?.file) {
      videoAssets.set(asset.id, { mediaId: asset.id, file: asset.file });
    }
  }
}

if (videoAssets.size > 0) {
  onProgress?.({ progress: 0.01 });
  const preflight = await videoCache.preflight({
    assets: [...videoAssets.values()],
  });

  if (!preflight.ok) {
    return {
      success: false,
      error: [
        "导出中止：以下素材无法解码视频画面（导出只会得到黑屏），请先转成 H.264(MP4) 或 VP9(WebM)：",
        ...preflight.failures.map((item) => `· ${item.name}：${item.reason}`),
      ].join("\\n"),
    };
  }
}
""")

sub_src(
    RENDERER_MANAGER,
    r"^([ \t]*)const canvasSize = activeProject\.settings\.canvasSize;",
    lambda m: m.group(0) + "\n" + indent_block(PREFLIGHT_BLOCK, 3),
    marker="const preflight = await videoCache.preflight({",
)

# 导出诊断必须在 buildScene 之前开启（素材缺失的记录发生在 buildScene 内部）
sub_src(
    RENDERER_MANAGER,
    r"^([ \t]*)const scene = buildScene\(\{",
    lambda m: m.group(1) + "videoCache.beginDiagnostics();\n\n" + m.group(0),
    marker="videoCache.beginDiagnostics();",
)

# 导出异常中止时也要收尾，避免诊断长期挂着把预览的取帧算进去
sub_src(
    RENDERER_MANAGER,
    r'^([ \t]*)console\.error\("Export failed:", error\);',
    lambda m: m.group(1) + "videoCache.endDiagnostics();\n" + m.group(0),
    marker="videoCache.endDiagnostics();",
)

print("  patched 导出健壮性 / 预检 / 取帧 cacheKey")
# ============================================================================
# 导出期决定分辨率 / 帧率 / 保存位置
#   对齐剪映的模型：工程里只存「基准画布 canvasSize」和「草稿帧率 fps」，
#   真正的输出尺寸/帧率在导出时由用户选择。渲染器保持节点数学以画布为单位，
#   只把绘制结果按比例映射到输出光栅。
# ============================================================================
print("==> 导出期分辨率 / 帧率 / 保存位置")

EXPORT_TYPES = SRC / "types" / "export.ts"
EXPORT_LIB = SRC / "lib" / "export.ts"
EXPORT_CONSTANTS = SRC / "constants" / "export-constants.ts"
EXPORT_BUTTON = SRC / "components" / "editor" / "export-button.tsx"
CANVAS_RENDERER = SRC / "services" / "renderer" / "canvas-renderer.ts"
SCENE_EXPORTER2 = SRC / "services" / "renderer" / "scene-exporter.ts"
RENDER_MANAGER = SRC / "core" / "managers" / "renderer-manager.ts"

# ------------------------------------------------------------ types/export.ts
sub_src(
    EXPORT_TYPES,
    r'^([ \t]*)export const EXPORT_FORMAT_VALUES = \["mp4", "webm"\] as const;',
    lambda m: m.group(0)
    + "\n\n"
    + tabsify("""
export const EXPORT_RESOLUTION_VALUES = [
  "project",
  "480p",
  "720p",
  "1080p",
  "2k",
  "4k",
] as const;

export const EXPORT_FPS_VALUES = [24, 25, 30, 50, 60] as const;
"""),
    marker="export const EXPORT_RESOLUTION_VALUES",
)

sub_src(
    EXPORT_TYPES,
    r"^([ \t]*)export type ExportFormat = \(typeof EXPORT_FORMAT_VALUES\)\[number\];",
    lambda m: m.group(0)
    + "\n"
    + m.group(1)
    + "export type ExportResolution = (typeof EXPORT_RESOLUTION_VALUES)[number];",
    marker="export type ExportResolution",
)

sub_src(
    EXPORT_TYPES,
    r"^([ \t]*)fps\?: number;",
    lambda m: m.group(0) + "\n" + m.group(1) + "resolution?: ExportResolution;",
    marker="resolution?: ExportResolution;",
)

# -------------------------------------------------------------- lib/export.ts
sub_src(
    EXPORT_LIB,
    r'^import type \{ ExportFormat \} from "@/types/export";',
    lambda m: 'import type { ExportFormat, ExportResolution } from "@/types/export";\n'
    'import { EXPORT_RESOLUTION_VALUES } from "@/types/export";',
    marker='import { EXPORT_RESOLUTION_VALUES } from "@/types/export";',
)

EXPORT_HELPERS = tabsify("""
export function resolveExportSize({
  canvasSize,
  resolution,
}: {
  canvasSize: { width: number; height: number };
  resolution?: ExportResolution;
}): { width: number; height: number } {
  if (!resolution || resolution === "project") {
    return { width: canvasSize.width, height: canvasSize.height };
  }

  const SHORT_SIDE: Record<string, number> = {
    "480p": 480,
    "720p": 720,
    "1080p": 1080,
    "2k": 1440,
    "4k": 2160,
  };
  const shortSide = SHORT_SIDE[resolution];
  if (!shortSide) {
    return { width: canvasSize.width, height: canvasSize.height };
  }

  const even = (value: number) => Math.max(2, Math.round(value / 2) * 2);
  const landscape = canvasSize.width >= canvasSize.height;

  // 横屏以高为短边，竖屏/方形以宽为短边
  const height = landscape
    ? shortSide
    : even((shortSide * canvasSize.height) / canvasSize.width);
  const width = landscape
    ? even((shortSide * canvasSize.width) / canvasSize.height)
    : shortSide;

  return { width, height };
}

export function getExportResolutionLabel({
  resolution,
  canvasSize,
}: {
  resolution: ExportResolution;
  canvasSize: { width: number; height: number };
}): string {
  const size = resolveExportSize({ canvasSize, resolution });

  if (resolution === "project") {
    return `${size.width}x${size.height} (project)`;
  }

  return `${resolution.toUpperCase()} (${size.width}x${size.height})`;
}

export function isExportResolution(value: string): value is ExportResolution {
  return EXPORT_RESOLUTION_VALUES.some((item) => item === value);
}

/**
 * 保存导出结果：支持 File System Access 的浏览器让用户选目录和文件名，
 * 不支持或出错时回退到普通下载（落到默认下载目录）。
 */
export async function saveExportBlob({
  blob,
  fileName,
  mimeType,
}: {
  blob: Blob;
  fileName: string;
  mimeType: string;
}): Promise<"saved" | "cancelled" | "downloaded"> {
  type FileSystemWritable = {
    write: (data: Blob) => Promise<void>;
    close: () => Promise<void>;
  };
  type FileSystemHandleLike = {
    createWritable: () => Promise<FileSystemWritable>;
  };
  type SavePicker = (options: {
    suggestedName?: string;
    types?: { description: string; accept: Record<string, string[]> }[];
  }) => Promise<FileSystemHandleLike>;

  const picker = (window as unknown as { showSaveFilePicker?: SavePicker })
    .showSaveFilePicker;

  if (typeof picker === "function") {
    try {
      const extension = fileName.slice(fileName.lastIndexOf("."));
      const handle = await picker({
        suggestedName: fileName,
        types: [{ description: "video", accept: { [mimeType]: [extension] } }],
      });
      const writable = await handle.createWritable();
      await writable.write(blob);
      await writable.close();
      return "saved";
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        return "cancelled";
      }
    }
  }

  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);

  return "downloaded";
}
""")

_lib_text = EXPORT_LIB.read_text(encoding="utf-8")
if "resolveExportSize" not in _lib_text:
    EXPORT_LIB.write_text(
        _lib_text.rstrip("\n") + "\n\n" + EXPORT_HELPERS + "\n", encoding="utf-8"
    )
    print(f"  patched {EXPORT_LIB.relative_to(ROOT)}")
else:
    print(f"  keep    {EXPORT_LIB.relative_to(ROOT)}")

# -------------------------------------------------- constants/export-constants
sub_src(
    EXPORT_CONSTANTS,
    r"([ \t]*)includeAudio: true,",
    lambda m: m.group(0) + "\n" + m.group(1) + 'resolution: "project",',
    marker='resolution: "project",',
)

# --------------------------------- canvas-renderer.ts：输出光栅与画布单位解耦
CANVAS_RENDERER_SOURCE = tabsify("""
import type { BaseNode } from "./nodes/base-node";

export type CanvasRendererParams = {
  width: number;
  height: number;
  fps: number;
  imageSmoothingQuality?: ImageSmoothingQuality;
  outputWidth?: number;
  outputHeight?: number;
};

export class CanvasRenderer {
  canvas: OffscreenCanvas | HTMLCanvasElement;
  context: OffscreenCanvasRenderingContext2D | CanvasRenderingContext2D;
  width: number;
  height: number;
  fps: number;
  outputWidth: number;
  outputHeight: number;
  private smoothingQuality: ImageSmoothingQuality;

  constructor({
    width,
    height,
    fps,
    imageSmoothingQuality = "low",
    outputWidth,
    outputHeight,
  }: CanvasRendererParams) {
    this.width = width;
    this.height = height;
    this.fps = fps;
    this.smoothingQuality = imageSmoothingQuality;
    // 输出光栅可以和画布单位不同：导出时用它决定真实像素尺寸
    this.outputWidth = outputWidth ?? width;
    this.outputHeight = outputHeight ?? height;

    this.canvas = this.createCanvas({
      width: this.outputWidth,
      height: this.outputHeight,
    });
    this.context = this.createContext();
    this.applyScale();
    this.applySmoothing();
  }

  private createCanvas({
    width,
    height,
  }: {
    width: number;
    height: number;
  }): OffscreenCanvas | HTMLCanvasElement {
    try {
      return new OffscreenCanvas(width, height);
    } catch {
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      return canvas;
    }
  }

  private createContext():
    | OffscreenCanvasRenderingContext2D
    | CanvasRenderingContext2D {
    const context = this.canvas.getContext("2d");
    if (!context) {
      throw new Error("Failed to get canvas context");
    }

    return context as
      | OffscreenCanvasRenderingContext2D
      | CanvasRenderingContext2D;
  }

  setSize({ width, height }: { width: number; height: number }) {
    const scaleX = this.outputWidth / this.width;
    const scaleY = this.outputHeight / this.height;

    this.width = width;
    this.height = height;
    this.outputWidth = Math.max(1, Math.round(width * scaleX));
    this.outputHeight = Math.max(1, Math.round(height * scaleY));
    this.canvas = this.createCanvas({
      width: this.outputWidth,
      height: this.outputHeight,
    });
    this.context = this.createContext();
    this.applyScale();
    this.applySmoothing();
  }

  /** 节点数学使用画布单位，这里把绘制结果映射到输出光栅。 */
  private applyScale() {
    const scaleX = this.outputWidth / this.width;
    const scaleY = this.outputHeight / this.height;
    this.context.setTransform(scaleX, 0, 0, scaleY, 0, 0);
  }

  private applySmoothing() {
    this.context.imageSmoothingEnabled = true;
    this.context.imageSmoothingQuality = this.smoothingQuality;
  }

  private clear() {
    this.context.fillStyle = "black";
    this.context.fillRect(0, 0, this.width, this.height);
  }

  async render({ node, time }: { node: BaseNode; time: number }) {
    // 每帧先重置基础变换，避免节点里 save/restore 不平衡把缩放丢掉
    this.applyScale();
    this.clear();
    await node.render({ renderer: this, time });
  }

  async renderToCanvas({
    node,
    time,
    targetCanvas,
  }: {
    node: BaseNode;
    time: number;
    targetCanvas: HTMLCanvasElement;
  }) {
    await this.render({ node, time });

    const ctx = targetCanvas.getContext("2d");
    if (!ctx) {
      throw new Error("Failed to get target canvas context");
    }

    ctx.drawImage(this.canvas, 0, 0, targetCanvas.width, targetCanvas.height);
  }
}
""")

if "outputWidth?: number;" not in CANVAS_RENDERER.read_text(encoding="utf-8"):
    write(CANVAS_RENDERER, CANVAS_RENDERER_SOURCE + "\n")
else:
    print(f"  keep    {CANVAS_RENDERER.relative_to(ROOT)}")

# -------------------------------------------------- scene-exporter：透传输出尺寸
sub_src(
    SCENE_EXPORTER2,
    r"([ \t]*)fps: number;\n([ \t]*)format: ExportFormat;",
    lambda m: m.group(1)
    + "fps: number;\n"
    + m.group(2)
    + "outputWidth?: number;\n"
    + m.group(2)
    + "outputHeight?: number;\n"
    + m.group(2)
    + "format: ExportFormat;",
    marker="outputWidth?: number;",
)

sub_src(
    SCENE_EXPORTER2,
    r"([ \t]*)width,\n([ \t]*)height,\n([ \t]*)fps,\n([ \t]*)format,\n"
    r"([ \t]*)quality,\n([ \t]*)shouldIncludeAudio,\n([ \t]*)audioBuffer,\n([ \t]*)\}: ExportParams\) \{",
    lambda m: m.group(1)
    + "width,\n"
    + m.group(2)
    + "height,\n"
    + m.group(3)
    + "fps,\n"
    + m.group(4)
    + "outputWidth,\n"
    + m.group(4)
    + "outputHeight,\n"
    + m.group(4)
    + "format,\n"
    + m.group(5)
    + "quality,\n"
    + m.group(6)
    + "shouldIncludeAudio,\n"
    + m.group(7)
    + "audioBuffer,\n"
    + m.group(8)
    + "}: ExportParams) {",
    marker="outputHeight,\n",
)

sub_src(
    SCENE_EXPORTER2,
    r"([ \t]*)this\.renderer = new CanvasRenderer\(\{\n"
    r"[ \t]*width,\n[ \t]*height,\n[ \t]*fps,\n"
    r"([ \t]*)imageSmoothingQuality: \"high\",",
    lambda m: m.group(1)
    + "this.renderer = new CanvasRenderer({\n"
    + m.group(2)
    + "// 输出光栅尺寸由导出选项决定，画布单位保持不变\n"
    + m.group(2)
    + "width,\n"
    + m.group(2)
    + "height,\n"
    + m.group(2)
    + "fps,\n"
    + m.group(2)
    + "outputWidth,\n"
    + m.group(2)
    + "outputHeight,\n"
    + m.group(2)
    + '// "low" 插值：导出缩小时比 "high" 快很多，肉眼几乎无差\n'
    + m.group(2)
    + 'imageSmoothingQuality: "low",',
    marker="// 输出光栅尺寸由导出选项决定，画布单位保持不变",
)


# ------------------------------------------ renderer-manager：输出尺寸与画布解耦
sub_src(
    RENDER_MANAGER,
    r'^import \{ getSelectedVideoClip \} from "@/lib/export";',
    lambda m: 'import {\n\tgetSelectedVideoClip,\n\tresolveExportSize,\n} from "@/lib/export";',
    marker="resolveExportSize,",
)

sub_src(
    RENDER_MANAGER,
    r"([ \t]*)const \{ format, quality, fps, includeAudio, onProgress, onCancel \} =\n[ \t]*options;",
    lambda m: m.group(1)
    + "const {\n"
    + m.group(1)
    + TAB
    + "format,\n"
    + m.group(1)
    + TAB
    + "quality,\n"
    + m.group(1)
    + TAB
    + "fps,\n"
    + m.group(1)
    + TAB
    + "resolution,\n"
    + m.group(1)
    + TAB
    + "includeAudio,\n"
    + m.group(1)
    + TAB
    + "onProgress,\n"
    + m.group(1)
    + TAB
    + "onCancel,\n"
    + m.group(1)
    + "} = options;",
    marker="\tresolution,\n",
)

sub_src(
    RENDER_MANAGER,
    r"([ \t]*)const canvasSize = activeProject\.settings\.canvasSize;",
    lambda m: m.group(0)
    + "\n"
    + m.group(1)
    + "// 输出尺寸只看导出选项，工程里的 canvasSize 只是基准画布\n"
    + m.group(1)
    + "const outputSize = resolveExportSize({ canvasSize, resolution });",
    marker="const outputSize = resolveExportSize(",
)

sub_src(
    RENDER_MANAGER,
    r"([ \t]*)width: canvasSize\.width,\n([ \t]*)height: canvasSize\.height,",
    lambda m: m.group(1)
    + "width: canvasSize.width,\n"
    + m.group(2)
    + "height: canvasSize.height,\n"
    + m.group(2)
    + "outputWidth: outputSize.width,\n"
    + m.group(2)
    + "outputHeight: outputSize.height,",
    marker="outputWidth: outputSize.width,",
)

# ------------------------------------------------------------ export-button.tsx
sub_src(
    EXPORT_BUTTON,
    r'^import \{ RadioGroup, RadioGroupItem \} from "@/components/ui/radio-group";',
    lambda m: m.group(0)
    + '\nimport {\n\tSelect,\n\tSelectContent,\n\tSelectItem,\n\tSelectTrigger,\n\tSelectValue,\n} from "@/components/ui/select";',
    marker='} from "@/components/ui/select";',
)

sub_src(
    EXPORT_BUTTON,
    r'^import \{ getExportMimeType, getExportFileExtension \} from "@/lib/export";',
    lambda m: "import {\n"
    "\tgetExportFileExtension,\n"
    "\tgetExportMimeType,\n"
    "\tgetExportResolutionLabel,\n"
    "\tisExportResolution,\n"
    "\tsaveExportBlob,\n"
    '} from "@/lib/export";',
    marker="saveExportBlob,",
)

sub_src(
    EXPORT_BUTTON,
    r"^([ \t]*)EXPORT_FORMAT_VALUES,\n([ \t]*)EXPORT_QUALITY_VALUES,",
    lambda m: m.group(1)
    + "EXPORT_FORMAT_VALUES,\n"
    + m.group(2)
    + "EXPORT_RESOLUTION_VALUES,\n"
    + m.group(2)
    + "EXPORT_QUALITY_VALUES,\n"
    + m.group(2)
    + "EXPORT_FPS_VALUES,",
    marker="EXPORT_RESOLUTION_VALUES,",
)

sub_src(
    EXPORT_BUTTON,
    r"([ \t]*)type ExportQuality,",
    lambda m: m.group(0) + "\n" + m.group(1) + "type ExportResolution,",
    marker="type ExportResolution,",
)

def indented_block(m, block: str) -> str:
    """把 2 空格缩进的代码块转成 tab 缩进，并按锚点行的缩进整体前移。"""
    return m.group(1) + tabsify(block).replace("\n", "\n" + m.group(1))


EXPORT_PANEL_STATE = """
const canvasSize = activeProject?.settings.canvasSize ?? {
  width: 1920,
  height: 1080,
};
const [resolution, setResolution] = useState<ExportResolution>(
  DEFAULT_EXPORT_OPTIONS.resolution,
);
const [fps, setFps] = useState<number>(
  Math.round(activeProject?.settings.fps ?? 30),
);
"""

EXPORT_PANEL_RESOLUTION_FPS = """
<PropertyGroup title={t("Resolution")} defaultExpanded={false}>
  <Select
    value={resolution}
    onValueChange={(value) => {
      if (isExportResolution(value)) {
        setResolution(value);
      }
    }}
  >
    <SelectTrigger className="h-8 w-full">
      <SelectValue />
    </SelectTrigger>
    <SelectContent className="z-[1200]">
      {EXPORT_RESOLUTION_VALUES.map((item) => (
        <SelectItem key={item} value={item}>
          {getExportResolutionLabel({ resolution: item, canvasSize })}
        </SelectItem>
      ))}
    </SelectContent>
  </Select>
</PropertyGroup>

<PropertyGroup title={t("Frame rate")} defaultExpanded={false}>
  <Select
    value={String(fps)}
    onValueChange={(value) => {
      const parsed = Number.parseInt(value, 10);
      if (Number.isFinite(parsed) && parsed > 0) {
        setFps(parsed);
      }
    }}
  >
    <SelectTrigger className="h-8 w-full">
      <SelectValue />
    </SelectTrigger>
    <SelectContent className="z-[1200]">
      {EXPORT_FPS_VALUES.map((item) => (
        <SelectItem key={item} value={String(item)}>
          {`${item} fps`}
        </SelectItem>
      ))}
    </SelectContent>
  </Select>
</PropertyGroup>
"""

sub_src(
    EXPORT_BUTTON,
    r"^([ \t]*)const \[isExporting, setIsExporting\] = useState\(false\);",
    lambda m: indented_block(m, EXPORT_PANEL_STATE) + "\n" + m.group(0),
    marker="const [resolution, setResolution]",
)

sub_src(
    EXPORT_BUTTON,
    r"^([ \t]*)fps: activeProject\.settings\.fps,",
    lambda m: m.group(1) + "fps,\n" + m.group(1) + "resolution,",
    marker="fps,\n" + TAB * 5 + "resolution,",
)

sub_src(
    EXPORT_BUTTON,
    r'([ \t]*)<PropertyGroup title=\{t\("Audio"\)\} defaultExpanded=\{false\}>',
    lambda m: indented_block(m, EXPORT_PANEL_RESOLUTION_FPS) + "\n" + m.group(0),
    marker='t("Resolution")',
)

# ------------------------------------------------- 导出完成后：选择保存位置
SAVE_BLOCK = tabsify("""
const mimeType = getExportMimeType({ format });
const extension = getExportFileExtension({ format });
const blob = new Blob([result.buffer], { type: mimeType });
const fileName = `${activeProject.metadata.name}${extension}`;

const saveResult = await saveExportBlob({ blob, fileName, mimeType });
if (saveResult === "cancelled") {
  // 用户放弃了选择位置：保留结果，面板不关，可以再点一次导出
  return;
}

onOpenChange(false);
""")

_button_text = EXPORT_BUTTON.read_text(encoding="utf-8")
if "saveExportBlob({ blob, fileName, mimeType })" not in _button_text:
    _start = _button_text.find("const mimeType = getExportMimeType({ format });")
    _end = _button_text.find("onOpenChange(false);", _start)
    if _start < 0 or _end < 0:
        raise SystemExit(f"save-block anchor not found in {EXPORT_BUTTON}")
    _indent = _button_text[_button_text.rfind("\n", 0, _start) + 1 : _start]
    _replacement = "\n".join(
        (_indent + line) if line.strip() else line for line in SAVE_BLOCK.split("\n")
    )
    EXPORT_BUTTON.write_text(
        _button_text[: _start - len(_indent)]
        + _replacement
        + _button_text[_end + len("onOpenChange(false);") :],
        encoding="utf-8",
    )
    print(f"  patched {EXPORT_BUTTON.relative_to(ROOT)}（保存位置选择）")
else:
    print(f"  keep    {EXPORT_BUTTON.relative_to(ROOT)}")


print("  patched 导出期分辨率 / 帧率 / 保存位置")

# ============================================================================
# 音轨独立编码 + 交织封装（对齐剪映：_temp_audio.aac + av_interleaved_write_frame）
#   浏览器没有 AAC 编码器时不再整体失败：MP4 先导出无声视频，同时把时间线音频
#   编成 WAV，交给 ComfyUI 侧的 ffmpeg 做「音轨编译 + 交织封装」。
# ============================================================================
print("==> 音视频合成（AAC 不可用时回退到本机 ffmpeg）")

# ------------------------------------------------------- types/export.ts: 结果
sub_src(
    EXPORT_TYPES,
    r"([ \t]*)export interface ExportResult \{\n([ \t]*)success: boolean;\n[ \t]*buffer\?: ArrayBuffer;",
    lambda m: m.group(1)
    + "export interface ExportResult {\n"
    + m.group(2)
    + "success: boolean;\n"
    + m.group(2)
    + "buffer?: ArrayBuffer;\n"
    + m.group(2)
    + "// 浏览器没有 AAC 编码器时，音频留给本机 ffmpeg 合成\n"
    + m.group(2)
    + "audioBuffer?: AudioBuffer;\n"
    + m.group(2)
    + "audioNeedsServerMux?: boolean;",
    marker="audioNeedsServerMux?: boolean;",
)

# --------------------------------------------------------- lib/export.ts: 工具
MUX_HELPERS = tabsify("""
/**
 * 探测浏览器能否真的编码音频。Linux 上的 Chromium 没有 AAC 编码器，
 * 这里会返回 null，由调用方回退到本机 ffmpeg 合成。
 */
export async function resolveSupportedAudioCodec({
  format,
  audioBuffer,
  bitrate = 192000,
}: {
  format: ExportFormat;
  audioBuffer: AudioBuffer;
  bitrate?: number;
}): Promise<"aac" | "opus" | null> {
  if (typeof AudioEncoder === "undefined") {
    return null;
  }

  const preferred = format === "webm" ? "opus" : "aac";
  const codec = preferred === "aac" ? "mp4a.40.2" : "opus";

  try {
    const { supported } = await AudioEncoder.isConfigSupported({
      codec,
      sampleRate: audioBuffer.sampleRate,
      numberOfChannels: audioBuffer.numberOfChannels,
      bitrate,
    });

    if (supported) {
      return preferred;
    }
  } catch {
    // 配置非法 / 不支持，统一走服务端合成
  }

  return null;
}

function writeAscii(view: DataView, offset: number, text: string) {
  for (let index = 0; index < text.length; index += 1) {
    view.setUint8(offset + index, text.charCodeAt(index));
  }
}

/** AudioBuffer -> 16bit PCM WAV（PCM 不需要任何编码器，处处可用）。 */
export function encodeAudioBufferToWav(audioBuffer: AudioBuffer): Blob {
  const channels = audioBuffer.numberOfChannels;
  const sampleRate = audioBuffer.sampleRate;
  const frames = audioBuffer.length;
  const blockAlign = channels * 2;
  const dataSize = frames * blockAlign;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);

  writeAscii(view, 0, "RIFF");
  view.setUint32(4, 36 + dataSize, true);
  writeAscii(view, 8, "WAVE");
  writeAscii(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, channels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * blockAlign, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, 16, true);
  writeAscii(view, 36, "data");
  view.setUint32(40, dataSize, true);

  const channelData: Float32Array[] = [];
  for (let channel = 0; channel < channels; channel += 1) {
    channelData.push(audioBuffer.getChannelData(channel));
  }

  let offset = 44;
  for (let frame = 0; frame < frames; frame += 1) {
    for (let channel = 0; channel < channels; channel += 1) {
      const source = channelData[channel];
      const sample = Math.max(-1, Math.min(1, source ? source[frame] : 0));
      view.setInt16(
        offset,
        sample < 0 ? sample * 0x8000 : sample * 0x7fff,
        true,
      );
      offset += 2;
    }
  }

  return new Blob([buffer], { type: "audio/wav" });
}

const MUX_CHUNK_SIZE = 8 * 1024 * 1024;

async function uploadMuxPart({
  token,
  name,
  blob,
}: {
  token: string;
  name: "video" | "audio" | "file";
  blob: Blob;
}): Promise<boolean> {
  const total = Math.max(1, Math.ceil(blob.size / MUX_CHUNK_SIZE));

  for (let index = 0; index < total; index += 1) {
    const chunk = blob.slice(
      index * MUX_CHUNK_SIZE,
      (index + 1) * MUX_CHUNK_SIZE,
    );
    const response = await fetch(
      `${withBasePath("/api/mux/part")}?token=${encodeURIComponent(token)}&name=${name}&index=${index}`,
      { method: "POST", body: chunk },
    );
    if (!response.ok) {
      return false;
    }
  }

  return true;
}

/**
 * 把无声视频 + 时间线音频交给 ComfyUI 侧的 ffmpeg 合成 MP4(AAC)。
 * 失败返回 null，调用方应继续给出无声视频而不是整体失败。
 */
export async function muxWithServer({
  videoBlob,
  audioBuffer,
  bitrateBps,
}: {
  videoBlob: Blob;
  audioBuffer: AudioBuffer;
  bitrateBps?: number;
}): Promise<Blob | null> {
  try {
    const begin = await fetch(withBasePath("/api/mux/begin"), {
      method: "POST",
    });
    if (!begin.ok) {
      return null;
    }

    const { token } = (await begin.json()) as { token?: string };
    if (!token) {
      return null;
    }

    if (!(await uploadMuxPart({ token, name: "video", blob: videoBlob }))) {
      return null;
    }

    const audioBlob = encodeAudioBufferToWav(audioBuffer);
    if (!(await uploadMuxPart({ token, name: "audio", blob: audioBlob }))) {
      return null;
    }

    const finished = await fetch(
      `${withBasePath("/api/mux/finish")}?token=${encodeURIComponent(token)}${bitrateBps ? `&bitrate=${Math.round(bitrateBps)}` : ""}`,
      { method: "POST" },
    );
    if (!finished.ok) {
      return null;
    }

    const merged = await finished.blob();
    return merged.size > 0 ? merged : null;
  } catch (error) {
    console.warn("[YuanCut] 服务端音频合成失败：", error);
    return null;
  }
}
""")

_lib_text = EXPORT_LIB.read_text(encoding="utf-8")
if "muxWithServer" not in _lib_text:
    _lib_text = _lib_text.rstrip("\n") + "\n\n" + MUX_HELPERS + "\n"
    EXPORT_LIB.write_text(_lib_text, encoding="utf-8")
    print(f"  patched {EXPORT_LIB.relative_to(ROOT)}")
else:
    print(f"  keep    {EXPORT_LIB.relative_to(ROOT)}")

sub_src(
    EXPORT_LIB,
    r'^import \{ EXPORT_RESOLUTION_VALUES \} from "@/types/export";',
    lambda m: m.group(0)
    + '\nimport { withBasePath } from "@/lib/base-path";',
    marker='from "@/lib/base-path";',
)

# ------------------------------------------------- scene-exporter：音频可跳过
sub_src(
    SCENE_EXPORTER2,
    r"([ \t]*)private isCancelled = false;",
    lambda m: m.group(0)
    + "\n"
    + m.group(1)
    + "private pendingAudioBuffer?: AudioBuffer;\n"
    + m.group(1)
    + "private audioRequiresServerMux = false;",
    marker="private pendingAudioBuffer?: AudioBuffer;",
)

sub_src(
    SCENE_EXPORTER2,
    r"([ \t]*)let audioSource: AudioBufferSource \| null = null;\n"
    r"([ \t]*)if \(this\.shouldIncludeAudio && this\.audioBuffer\) \{\n"
    r"[ \t]*audioSource = new AudioBufferSource\(\{\n"
    r"[ \t]*codec: this\.format === \"webm\" \? \"opus\" : \"aac\",\n"
    r"[ \t]*bitrate: qualityMap\[this\.quality\],\n"
    r"[ \t]*\}\);\n"
    r"[ \t]*output\.addAudioTrack\(audioSource\);\n"
    r"[ \t]*\}",
    lambda m: (
        m.group(1)
        + "let audioSource: AudioBufferSource | null = null;\n"
        + m.group(1)
        + "if (this.shouldIncludeAudio && this.audioBuffer) {\n"
        + m.group(1)
        + TAB
        + "// 浏览器可能根本没有音频编码器（Linux 上没有 AAC）\n"
        + m.group(1)
        + TAB
        + "const audioCodec = await resolveSupportedAudioCodec({\n"
        + m.group(1)
        + TAB * 2
        + "format: this.format,\n"
        + m.group(1)
        + TAB * 2
        + "audioBuffer: this.audioBuffer,\n"
        + m.group(1)
        + TAB
        + "});\n\n"
        + m.group(1)
        + TAB
        + "if (audioCodec) {\n"
        + m.group(1)
        + TAB * 2
        + "audioSource = new AudioBufferSource({\n"
        + m.group(1)
        + TAB * 3
        + "codec: audioCodec,\n"
        + m.group(1)
        + TAB * 3
        + "bitrate: qualityMap[this.quality],\n"
        + m.group(1)
        + TAB * 2
        + "});\n"
        + m.group(1)
        + TAB * 2
        + "output.addAudioTrack(audioSource);\n"
        + m.group(1)
        + TAB
        + "} else {\n"
        + m.group(1)
        + TAB * 2
        + "// 先出无声视频，音频交给本机 ffmpeg 合成（音轨独立编译 + 交织封装）\n"
        + m.group(1)
        + TAB * 2
        + "this.pendingAudioBuffer = this.audioBuffer;\n"
        + m.group(1)
        + TAB * 2
        + "this.audioRequiresServerMux = true;\n"
        + m.group(1)
        + TAB
        + "}\n"
        + m.group(1)
        + "}"
    ),
    marker="this.audioRequiresServerMux = true;",
)

sub_src(
    SCENE_EXPORTER2,
    r"([ \t]*)const \{ fps \} = this\.renderer;",
    lambda m: m.group(0)
    + "\n"
    + m.group(1)
    + "this.pendingAudioBuffer = undefined;\n"
    + m.group(1)
    + "this.audioRequiresServerMux = false;",
    marker="this.audioRequiresServerMux = false;",
)

ENCODING_FALLBACK_METHOD = """
/** 选一个本机真能用的编码器配置：所选不可用时按「同编码器软编 -> 其它编码器」回退。 */
private async resolveVideoEncodingConfig(): Promise<{
  codec: VideoCodec;
  hardwareAcceleration: "no-preference" | "prefer-hardware" | "prefer-software";
}> {
  const preferredCodec: VideoCodec =
    this.videoCodec ?? (this.format === "webm" ? "vp9" : "avc");
  const preferredHardware = this.hardwareAcceleration ?? "prefer-software";

  const candidates: VideoCodec[] =
    this.format === "webm" ? ["vp9", "av1"] : ["avc", "av1", "hevc"];
  const codecs = [preferredCodec, ...candidates].filter(
    (codec, index, all) => all.indexOf(codec) === index,
  );

  const hardwareOptions: (
    | "no-preference"
    | "prefer-hardware"
    | "prefer-software"
  )[] = [preferredHardware, "prefer-software", "no-preference"];
  const uniqueHardware = hardwareOptions.filter(
    (item, index, all) => all.indexOf(item) === index,
  );

  for (const codec of codecs) {
    for (const hardwareAcceleration of uniqueHardware) {
      const supported = await canEncodeVideo(codec, {
        width: this.renderer.outputWidth,
        height: this.renderer.outputHeight,
        frameRate: this.renderer.fps,
        hardwareAcceleration,
      });

      if (!supported) continue;

      if (
        codec !== preferredCodec ||
        hardwareAcceleration !== preferredHardware
      ) {
        console.warn(
          `[YuanCut] 编码器回退：${preferredCodec}(${preferredHardware}) 不可用，已改用 ${codec}(${hardwareAcceleration})`,
        );
      }

      return { codec, hardwareAcceleration };
    }
  }

  throw new Error(
    `当前浏览器没有可用的视频编码器（已尝试 ${codecs.join("/")}，含软编与硬编）。请改用 WebM 格式或降低分辨率后重试。`,
  );
}

"""

sub_src(
    SCENE_EXPORTER2,
    r"([ \t]*)cancel\(\): void \{",
    lambda m: m.group(1)
    + "getPendingExportAudio(): AudioBuffer | undefined {\n"
    + m.group(1)
    + TAB
    + "return this.pendingAudioBuffer;\n"
    + m.group(1)
    + "}\n\n"
    + m.group(1)
    + "get requiresServerAudioMux(): boolean {\n"
    + m.group(1)
    + TAB
    + "return this.audioRequiresServerMux;\n"
    + m.group(1)
    + "}\n\n"
    + m.group(1)
    + "cancel(): void {",
    marker="getPendingExportAudio(): AudioBuffer | undefined {",
)

sub_src(
    SCENE_EXPORTER2,
    r"([ \t]*)cancel\(\): void \{",
    lambda m: indent_block(
        tabsify(ENCODING_FALLBACK_METHOD),
        1,
    ).replace("\n", "\n" + m.group(1))
    + m.group(1)
    + "cancel(): void {",
    marker="private async resolveVideoEncodingConfig()",
)

sub_src(
    SCENE_EXPORTER2,
    r"([ \t]*)QUALITY_VERY_HIGH,\n([ \t]*)\} from \"mediabunny\";",
    lambda m: m.group(1)
    + "QUALITY_VERY_HIGH,\n"
    + m.group(1)
    + "canEncodeVideo,\n"
    + m.group(2)
    + '} from "mediabunny";\n'
    + 'import type { VideoCodec } from "mediabunny";',
    marker="canEncodeVideo,",
)

sub_src(
    SCENE_EXPORTER2,
    r'^import \{ videoCache \} from "@/services/video-cache/service";',
    lambda m: m.group(0)
    + '\nimport { resolveSupportedAudioCodec } from "@/lib/export";',
    marker="resolveSupportedAudioCodec } from",
)

# ---------------------------------------- renderer-manager：把待合成音频带出来
sub_src(
    RENDER_MANAGER,
    r"([ \t]*)return \{\n([ \t]*)success: true,\n([ \t]*)buffer,\n([ \t]*)\};",
    lambda m: m.group(1)
    + "return {\n"
    + m.group(2)
    + "success: true,\n"
    + m.group(3)
    + "buffer,\n"
    + m.group(3)
    + "audioBuffer: exporter.getPendingExportAudio(),\n"
    + m.group(3)
    + "audioNeedsServerMux: exporter.requiresServerAudioMux,\n"
    + m.group(4)
    + "};",
    marker="audioNeedsServerMux: exporter.requiresServerAudioMux,",
)

# ------------------------------------------------- export-button：合成后再保存
sub_src(
    EXPORT_BUTTON,
    r"^([ \t]*)const saveResult = await saveExportBlob\(\{ blob, fileName, mimeType \}\);",
    lambda m: m.group(1)
    + "let outputBlob = blob;\n"
    + m.group(1)
    + "if (format === \"mp4\" && result.audioNeedsServerMux && result.audioBuffer) {\n"
    + m.group(1)
    + TAB
    + "// 浏览器编不了 AAC：把无声视频 + 时间线音频交给 ComfyUI 侧 ffmpeg 合成\n"
    + m.group(1)
    + TAB
    + "const merged = await muxWithServer({\n"
    + m.group(1)
    + TAB * 2
    + "videoBlob: blob,\n"
    + m.group(1)
    + TAB * 2
    + "audioBuffer: result.audioBuffer,\n"
    + m.group(1)
    + TAB * 2
    + "bitrateBps:\n"
    + m.group(1)
    + TAB * 3
    + "bitrateMbps > 0 ? Math.round(bitrateMbps * 1_000_000) : undefined,\n"
    + m.group(1)
    + TAB
    + "});\n\n"
    + m.group(1)
    + TAB
    + "if (merged) {\n"
    + m.group(1)
    + TAB * 2
    + "outputBlob = merged;\n"
    + m.group(1)
    + TAB
    + "} else {\n"
    + m.group(1)
    + TAB * 2
    + 'console.warn("[YuanCut] 服务端音频合成失败，已保留无声视频");\n'
    + m.group(1)
    + TAB
    + "}\n"
    + m.group(1)
    + "}\n\n"
    + m.group(1)
    + "const saveResult = await saveExportBlob({\n"
    + m.group(1)
    + TAB
    + "blob: outputBlob,\n"
    + m.group(1)
    + TAB
    + "fileName,\n"
    + m.group(1)
    + TAB
    + "mimeType,\n"
    + m.group(1)
    + "});",
    marker="const merged = await muxWithServer({",
)

sub_src(
    EXPORT_BUTTON,
    r"([ \t]*)saveExportBlob,\n([ \t]*)\} from \"@/lib/export\";",
    lambda m: m.group(1)
    + "muxWithServer,\n"
    + m.group(1)
    + "saveExportBlob,\n"
    + m.group(2)
    + '} from "@/lib/export";',
    marker="muxWithServer,\n",
)

print("  patched 音视频合成（AAC 回退到本机 ffmpeg）")

# ============================================================================
# 三段式导出面板：视频 / 音频 / 字幕各自可选，勾选后才可编辑对应参数
#   - 绿色画面修复：导出画布强制不透明（透明像素被编码成绿色）
#   - 视频：分辨率 / 码率 / 编码 / 格式 / 帧率
#   - 音频：WAV（浏览器）/ MP3、AAC（本机 ffmpeg）
#   - 字幕：SRT / TXT（从时间线文字元素生成）
#   - 同名不同扩展名，统一写入所选目录
# ============================================================================
print("==> 三段式导出面板（视频 / 音频 / 字幕）")

# ------------------------------------------------------------------ 类型扩展
sub_src(
    EXPORT_TYPES,
    r"^([ \t]*)export const EXPORT_FPS_VALUES = \[24, 25, 30, 50, 60\] as const;",
    lambda m: m.group(0)
    + "\n\n"
    + tabsify("""
export const EXPORT_VIDEO_CODEC_VALUES = ["avc", "hevc", "vp9", "av1"] as const;
export type ExportVideoCodec = (typeof EXPORT_VIDEO_CODEC_VALUES)[number];

/** avc 就是 H.264，hevc 就是 H.265 —— 面板上用通用叫法，避免用户找不到 H.264。 */
export const EXPORT_VIDEO_CODEC_LABELS: Record<ExportVideoCodec, string> = {
  avc: "H.264 (AVC)",
  hevc: "H.265 (HEVC)",
  vp9: "VP9",
  av1: "AV1",
};

/** 0 表示按质量自动；其余为 Mbps */
export const EXPORT_BITRATE_VALUES = [0, 4, 8, 16, 32] as const;

export const EXPORT_AUDIO_FORMAT_VALUES = ["wav", "mp3", "aac"] as const;
export type ExportAudioFormat = (typeof EXPORT_AUDIO_FORMAT_VALUES)[number];

export const EXPORT_SUBTITLE_FORMAT_VALUES = ["srt", "txt"] as const;
export type ExportSubtitleFormat = (typeof EXPORT_SUBTITLE_FORMAT_VALUES)[number];
"""),
    marker="EXPORT_VIDEO_CODEC_VALUES",
)

sub_src(
    EXPORT_TYPES,
    r"^([ \t]*)resolution\?: ExportResolution;",
    lambda m: m.group(0)
    + "\n"
    + m.group(1)
    + "videoCodec?: ExportVideoCodec;\n"
    + m.group(1)
    + "bitrateMbps?: number;\n"
    + m.group(1)
    + "hardwareAcceleration?: \"no-preference\" | \"prefer-hardware\" | \"prefer-software\";",
    marker="videoCodec?: ExportVideoCodec;",
)

# ------------------------------------------- canvas-renderer：导出画布不透明
sub_src(
    CANVAS_RENDERER,
    r"([ \t]*)const context = this\.canvas\.getContext\(\"2d\"\);",
    lambda m: m.group(1)
    + "// alpha: false — 透明像素会被 H.264 编码成绿色画面\n"
    + m.group(1)
    + 'const context = this.canvas.getContext("2d", { alpha: false });',
    marker="{ alpha: false }",
)

# ------------------------------- scene-exporter：编码器 / 码率 / 硬件加速透传
sub_src(
    SCENE_EXPORTER2,
    r"([ \t]*)outputWidth\?: number;\n([ \t]*)outputHeight\?: number;",
    lambda m: m.group(0)
    + "\n"
    + m.group(2)
    + "videoCodec?: ExportVideoCodec;\n"
    + m.group(2)
    + "bitrateMbps?: number;\n"
    + m.group(2)
    + 'hardwareAcceleration?: "no-preference" | "prefer-hardware" | "prefer-software";',
    marker="videoCodec?: ExportVideoCodec;",
)

sub_src(
    SCENE_EXPORTER2,
    r"([ \t]*)outputWidth,\n([ \t]*)outputHeight,\n([ \t]*)format,",
    lambda m: m.group(1)
    + "outputWidth,\n"
    + m.group(2)
    + "outputHeight,\n"
    + m.group(2)
    + "videoCodec,\n"
    + m.group(2)
    + "bitrateMbps,\n"
    + m.group(2)
    + "hardwareAcceleration,\n"
    + m.group(3)
    + "format,",
    marker="bitrateMbps,\n",
)

sub_src(
    SCENE_EXPORTER2,
    r"([ \t]*)private format: ExportFormat;\n([ \t]*)private quality: ExportQuality;",
    lambda m: m.group(1)
    + "private format: ExportFormat;\n"
    + m.group(2)
    + "private quality: ExportQuality;\n"
    + m.group(2)
    + "private videoCodec?: ExportVideoCodec;\n"
    + m.group(2)
    + "private bitrateMbps?: number;\n"
    + m.group(2)
    + 'private hardwareAcceleration?: "no-preference" | "prefer-hardware" | "prefer-software";',
    marker="private videoCodec?: ExportVideoCodec;",
)

sub_src(
    SCENE_EXPORTER2,
    r"([ \t]*)this\.format = format;\n([ \t]*)this\.quality = quality;",
    lambda m: m.group(1)
    + "this.format = format;\n"
    + m.group(2)
    + "this.quality = quality;\n"
    + m.group(2)
    + "this.videoCodec = videoCodec;\n"
    + m.group(2)
    + "this.bitrateMbps = bitrateMbps;\n"
    + m.group(2)
    + "this.hardwareAcceleration = hardwareAcceleration;",
    marker="this.videoCodec = videoCodec;",
)

sub_src(
    SCENE_EXPORTER2,
    r"([ \t]*)const videoSource = new CanvasSource\(this\.renderer\.canvas, \{\n"
    r"[ \t]*codec: this\.format === \"webm\" \? \"vp9\" : \"avc\",\n"
    r"[ \t]*bitrate: qualityMap\[this\.quality\],\n"
    r"[ \t]*\}\);",
    lambda m: m.group(1)
    + "// 逐级回退：所选编码器/硬件加速不可用时自动降级（对齐剪映的重试策略）\n"
    + m.group(1)
    + "const encoding = await this.resolveVideoEncodingConfig();\n\n"
    + m.group(1)
    + "const videoSource = new CanvasSource(this.renderer.canvas, {\n"
    + m.group(1)
    + TAB
    + "codec: encoding.codec,\n"
    + m.group(1)
    + TAB
    + "bitrate: this.bitrateMbps\n"
    + m.group(1)
    + TAB * 2
    + "? Math.round(this.bitrateMbps * 1_000_000)\n"
    + m.group(1)
    + TAB * 2
    + ": qualityMap[this.quality],\n"
    + m.group(1)
    + TAB
    + "hardwareAcceleration: encoding.hardwareAcceleration,\n"
    + m.group(1)
    + TAB
    + "// 关键帧间隔放大：编码更快、文件更小\n"
    + m.group(1)
    + TAB
    + "keyFrameInterval: 8,\n"
    + m.group(1)
    + "});",
    marker="await this.resolveVideoEncodingConfig();",
)

sub_src(
    SCENE_EXPORTER2,
    r'^import type \{ RootNode \} from "\./nodes/root-node";',
    lambda m: m.group(0)
    + '\nimport type { ExportVideoCodec } from "@/types/export";',
    marker="import type { ExportVideoCodec }",
)

# --------------------------------- renderer-manager：把视频编码选项继续往下传
sub_src(
    RENDER_MANAGER,
    r"([ \t]*)resolution,\n([ \t]*)includeAudio,",
    lambda m: m.group(1)
    + "resolution,\n"
    + m.group(1)
    + "videoCodec,\n"
    + m.group(1)
    + "bitrateMbps,\n"
    + m.group(1)
    + "hardwareAcceleration,\n"
    + m.group(2)
    + "includeAudio,",
    marker="videoCodec,\n",
)

sub_src(
    RENDER_MANAGER,
    r"([ \t]*)outputHeight: outputSize\.height,",
    lambda m: m.group(0)
    + "\n"
    + m.group(1)
    + "videoCodec,\n"
    + m.group(1)
    + "bitrateMbps,\n"
    + m.group(1)
    + "hardwareAcceleration,",
    marker="outputHeight: outputSize.height,\n" + TAB * 4 + "videoCodec,",
)

print("  patched 绿色画面修复 + 编码器/码率透传")

# ------------------------------------------------ lib/export.ts：字幕 + 音频转码
sub_src(
    EXPORT_LIB,
    r'^import type \{ ExportFormat, ExportResolution \} from "@/types/export";',
    lambda m: 'import type {\n'
    "\tExportAudioFormat,\n"
    "\tExportFormat,\n"
    "\tExportResolution,\n"
    "\tExportSubtitleFormat,\n"
    '} from "@/types/export";\n'
    'import type { TextElement } from "@/types/timeline";',
    marker="ExportSubtitleFormat,",
)

SUBTITLE_HELPERS = tabsify("""
function padTime(value: number, size = 2): string {
  return String(Math.floor(value)).padStart(size, "0");
}
function formatSrtTime(seconds: number): string {
  // 先四舍五入到毫秒再拆位，避免出现 ",1000" 这种越界毫秒（导入侧会解析成 1 秒，
  // 但很多播放器会直接判为坏时间码）
  const totalMs = Math.round(Math.max(0, seconds) * 1000);
  const hours = Math.floor(totalMs / 3600000);
  const minutes = Math.floor((totalMs % 3600000) / 60000);
  const secs = Math.floor((totalMs % 60000) / 1000);
  const millis = totalMs % 1000;
  return `${padTime(hours)}:${padTime(minutes)}:${padTime(secs)},${padTime(millis, 3)}`;
}

/** 从时间线上的文字元素生成字幕文件内容（SRT / 纯文本）。 */
export function buildSubtitleFile({
  tracks,
  format,
}: {
  tracks: { elements: { type: string }[] }[];
  format: ExportSubtitleFormat;
}): string {
  const cues: { start: number; end: number; text: string }[] = [];

  for (const track of tracks) {
    for (const element of track.elements) {
      if (element.type !== "text") continue;

      const textElement = element as unknown as TextElement;
      if (textElement.hidden) continue;

      const text = textElement.content?.trim();
      if (!text) continue;

      const start = Math.max(0, textElement.startTime);
      const end = start + Math.max(0, textElement.duration);
      cues.push({ start, end, text });
    }
  }

  cues.sort((left, right) => left.start - right.start);

  if (format === "txt") {
    return `${cues.map((cue) => cue.text).join("\\n")}\\n`;
  }

  const blocks = cues.map(
    (cue, index) =>
      `${index + 1}\\n${formatSrtTime(cue.start)} --> ${formatSrtTime(cue.end)}\\n${cue.text}`,
  );

  return `${blocks.join("\\n\\n")}\\n`;
}

/** WAV -> MP3 / AAC：浏览器没有这些编码器，交给 ComfyUI 侧的 ffmpeg。 */
export async function convertAudioWithServer({
  audioBlob,
  format,
}: {
  audioBlob: Blob;
  format: ExportAudioFormat;
}): Promise<Blob | null> {
  if (format !== "mp3" && format !== "aac") {
    return null;
  }

  try {
    const begin = await fetch(withBasePath("/api/mux/begin"), {
      method: "POST",
    });
    if (!begin.ok) return null;

    const { token } = (await begin.json()) as { token?: string };
    if (!token) return null;

    if (!(await uploadMuxPart({ token, name: "audio", blob: audioBlob }))) {
      return null;
    }

    const response = await fetch(
      `${withBasePath("/api/mux/audio")}?token=${encodeURIComponent(token)}&format=${format}`,
      { method: "POST" },
    );
    if (!response.ok) return null;

    const blob = await response.blob();
    return blob.size > 0 ? blob : null;
  } catch (error) {
    console.warn("[YuanCut] 音频转码失败：", error);
    return null;
  }
}
""")

_lib_text = EXPORT_LIB.read_text(encoding="utf-8")
if "buildSubtitleFile" not in _lib_text:
    EXPORT_LIB.write_text(
        _lib_text.rstrip("\n") + "\n\n" + SUBTITLE_HELPERS + "\n", encoding="utf-8"
    )
    print(f"  patched {EXPORT_LIB.relative_to(ROOT)}")
else:
    print(f"  keep    {EXPORT_LIB.relative_to(ROOT)}")

# 编码器可用性探测（avc 即 H.264）：按容器 + 浏览器实际能力过滤
sub_src(
    EXPORT_LIB,
    r'^import type \{\n([ \t]*)ExportAudioFormat,',
    lambda m: 'import type {\n'
    + m.group(1)
    + "ExportAudioFormat,\n"
    + m.group(1)
    + "ExportVideoCodec,",
    marker="ExportVideoCodec,",
)


CODEC_HELPERS = tabsify("""
/** 容器允许的编码器：MP4 走 AVC(HEVC)/AV1，WebM 只认 VP9/AV1。 */
export function isCodecAllowedForContainer({
  codec,
  format,
}: {
  codec: ExportVideoCodec;
  format: ExportFormat;
}): boolean {
  if (format === "webm") {
    return codec === "vp9" || codec === "av1";
  }

  return codec === "avc" || codec === "hevc" || codec === "av1";
}

export function isExportVideoCodec(value: string): value is ExportVideoCodec {
  return EXPORT_VIDEO_CODEC_VALUES.some((item) => item === value);
}

export function getCodecLabel(codec: ExportVideoCodec): string {
  return EXPORT_VIDEO_CODEC_LABELS[codec];
}

/**
 * 探测当前浏览器真正能编码的视频编码器（avc 就是 H.264）。
 * 面板据此过滤下拉项，避免选到编不出来的组合。
 */
export async function getAvailableVideoCodecs({
  format,
  width,
  height,
}: {
  format: ExportFormat;
  width: number;
  height: number;
}): Promise<ExportVideoCodec[]> {
  const candidates = EXPORT_VIDEO_CODEC_VALUES.filter((codec) =>
    isCodecAllowedForContainer({ codec, format }),
  );

  try {
    const encodable = await getEncodableVideoCodecs(candidates, {
      width,
      height,
    });
    const allowed = encodable.filter((codec): codec is ExportVideoCodec =>
      isExportVideoCodec(codec),
    );

    if (allowed.length > 0) {
      return allowed;
    }
  } catch {
    // 探测失败（旧内核等）就退回保守默认
  }

  return format === "webm" ? ["vp9"] : ["avc"];
}

/** ComfyUI 侧的导出目录绝对路径（面板上要显示给用户看的那个）。 */
export async function getServerExportDir(): Promise<string | null> {
  try {
    const response = await fetch(withBasePath("/api/export-dir"), {
      cache: "no-store",
    });
    if (!response.ok) return null;

    const data = (await response.json()) as { path?: string };
    return data.path ?? null;
  } catch {
    return null;
  }
}

/** 把任意文件直接写进 ComfyUI 侧的导出目录，返回落盘的绝对路径。 */
/** 上传一个素材到 ComfyUI 侧缓存（按 assetId 去重，供服务端渲染引用）。 */
export async function uploadAssetBlob({
  assetId,
  blob,
  ext,
}: {
  assetId: string;
  blob: Blob;
  ext: string;
}): Promise<boolean> {
  try {
    const begin = await fetch(withBasePath("/api/asset/begin"), { method: "POST" });
    if (!begin.ok) return false;

    const { token } = (await begin.json()) as { token?: string };
    if (!token) return false;

    const total = Math.max(1, Math.ceil(blob.size / MUX_CHUNK_SIZE));
    for (let index = 0; index < total; index += 1) {
      const chunk = blob.slice(
        index * MUX_CHUNK_SIZE,
        (index + 1) * MUX_CHUNK_SIZE,
      );
      const response = await fetch(
        `${withBasePath("/api/asset/part")}?token=${encodeURIComponent(token)}&index=${index}`,
        { method: "POST", body: chunk },
      );
      if (!response.ok) return false;
    }

    const finished = await fetch(
      `${withBasePath("/api/asset/finish")}?token=${encodeURIComponent(token)}&assetId=${encodeURIComponent(assetId)}&ext=${encodeURIComponent(ext)}`,
      { method: "POST" },
    );
    return finished.ok;
  } catch (error) {
    console.warn("[YuanCut] 素材上传失败：", error);
    return false;
  }
}

export async function saveBlobToServer({
  blob,
  fileName,
}: {
  blob: Blob;
  fileName: string;
}): Promise<string | null> {
  try {
    const begin = await fetch(withBasePath("/api/mux/begin"), {
      method: "POST",
    });
    if (!begin.ok) return null;

    const { token } = (await begin.json()) as { token?: string };
    if (!token) return null;

    if (!(await uploadMuxPart({ token, name: "file", blob }))) {
      return null;
    }

    const response = await fetch(
      `${withBasePath("/api/save/finish")}?token=${encodeURIComponent(token)}&filename=${encodeURIComponent(fileName)}`,
      { method: "POST" },
    );
    if (!response.ok) return null;

    const data = (await response.json()) as { path?: string };
    return data.path ?? null;
  } catch (error) {
    console.warn("[YuanCut] 服务端保存失败：", error);
    return null;
  }
}
""")

_lib_text = EXPORT_LIB.read_text(encoding="utf-8")
if "getAvailableVideoCodecs" not in _lib_text:
    _lib_text = _lib_text.rstrip("\n") + "\n\n" + CODEC_HELPERS + "\n"
    if "from \"mediabunny\"" not in _lib_text:
        _lib_text = (
            'import { getEncodableVideoCodecs } from "mediabunny";\n' + _lib_text
        )
    if 'import { EXPORT_RESOLUTION_VALUES } from "@/types/export";' in _lib_text:
        _lib_text = _lib_text.replace(
            'import { EXPORT_RESOLUTION_VALUES } from "@/types/export";',
            'import {\n'
            "\tEXPORT_RESOLUTION_VALUES,\n"
            "\tEXPORT_VIDEO_CODEC_LABELS,\n"
            "\tEXPORT_VIDEO_CODEC_VALUES,\n"
            '} from "@/types/export";',
            1,
        )
    EXPORT_LIB.write_text(_lib_text, encoding="utf-8")
    print(f"  patched {EXPORT_LIB.relative_to(ROOT)}")
else:
    print(f"  keep    {EXPORT_LIB.relative_to(ROOT)}")

# ------------------------------------------- export-button.tsx：整文件重写
EXPORT_BUTTON_SOURCE = tabsify("""
"use client";

import { useEffect, useRef, useState } from "react";
import { TransitionTopIcon } from "@hugeicons/core-free-icons";
import { HugeiconsIcon } from "@hugeicons/react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/utils/ui";
import { Download, FolderOpen, RotateCcw } from "lucide-react";
import {
  buildSubtitleFile,
  convertAudioWithServer,
  encodeAudioBufferToWav,
  getAvailableVideoCodecs,
  getCodecLabel,
  getExportFileExtension,
  getExportResolutionLabel,
  getServerExportDir,
  saveBlobToServer,
  isExportResolution,
  isExportVideoCodec,
  resolveExportSize,
  uploadAssetBlob,
  saveExportBlob,
} from "@/lib/export";
import { createTimelineAudioBuffer } from "@/lib/media/audio";
import { withBasePath } from "@/lib/base-path";
import {
  EXPORT_AUDIO_FORMAT_VALUES,
  EXPORT_BITRATE_VALUES,
  EXPORT_FPS_VALUES,
  EXPORT_FORMAT_VALUES,
  EXPORT_RESOLUTION_VALUES,
  EXPORT_SUBTITLE_FORMAT_VALUES,
  type ExportAudioFormat,
  type ExportFormat,
  type ExportResolution,
  type ExportResult,
  type ExportSubtitleFormat,
  type ExportVideoCodec,
} from "@/types/export";
import { useEditor } from "@/hooks/use-editor";
import { useTranslation } from "@i18next-toolkit/nextjs-approuter";

type DirectoryHandleLike = {
  name?: string;
  getFileHandle: (
    name: string,
    options?: { create?: boolean },
  ) => Promise<{
    createWritable: () => Promise<{
      write: (data: Blob) => Promise<void>;
      close: () => Promise<void>;
    }>;
  }>;
};

type ExportFile = { name: string; blob: Blob };

/**
 * 面板文案：宿主应用的 i18n 用的是哈希键，新增文案取不到译文，
 * 这里按当前语言直接给中文，避免"导出功能没汉化"。
 */
const EXPORT_LABELS_ZH: Record<string, string> = {
  Export: "导出",
  "Export project": "导出项目",
  "Exporting project": "正在导出…",
  "Export failed": "导出失败",
  Retry: "重试",
  "File name": "导出名称",
  "Export folder": "导出目录",
  Choose: "选择…",
  "Choose a folder in this browser instead": "改为在浏览器里选择目录",
  "Browser default download folder": "浏览器默认下载目录",
  "Files are written to this folder on the ComfyUI host.":
    "文件会直接写入本机这个目录。",
  "Not supported": "当前浏览器不支持选择目录",
  "Video export": "视频导出",
  "Audio export": "音频导出",
  "Subtitle export": "字幕导出",
  Format: "格式",
  Resolution: "分辨率",
  "Frame rate": "帧率",
  Codec: "编码",
  Bitrate: "码率",
  "Hardware encoding": "硬件加速编码",
  "Off (software)": "关闭（软件编码）",
  "On (hardware)": "开启（硬件编码）",
  Auto: "自动",
  Cancel: "取消",
  "Exported successfully": "导出成功",
  "Output folder": "存放位置",
  Close: "关闭",
  "File saved to the browser download folder": "文件已保存到浏览器下载目录",
};

function isChineseUi(): boolean {
  if (typeof window === "undefined") return true;
  return window.location.pathname.split("/").includes("zh");
}

function useChineseLabels({
  translate,
}: {
  translate: (key: string) => string;
}) {
  return (key: string): string => {
    if (isChineseUi()) {
      const label = EXPORT_LABELS_ZH[key];
      if (label) return label;
    }
    return translate(key);
  };
}

export function ExportButton() {
  const { t } = useTranslation();
  const [isExportPopoverOpen, setIsExportPopoverOpen] = useState(false);
  const editor = useEditor();

  const handleExport = () => {
    setIsExportPopoverOpen(true);
  };

  const hasProject = !!editor.project.getActive();

  return (
    <Dialog open={isExportPopoverOpen} onOpenChange={setIsExportPopoverOpen}>
      <DialogTrigger asChild>
        <button
          type="button"
          className={cn(
            "flex items-center gap-1.5 rounded-lg border border-border bg-white px-3.5 py-1.5 text-sm font-medium text-gray-800 shadow-sm transition-colors hover:bg-gray-50 active:bg-gray-100",
            hasProject ? "cursor-pointer" : "cursor-not-allowed opacity-50",
          )}
          onClick={hasProject ? handleExport : undefined}
          disabled={!hasProject}
          onKeyDown={(event) => {
            if (hasProject && (event.key === "Enter" || event.key === " ")) {
              event.preventDefault();
              handleExport();
            }
          }}
        >
          <HugeiconsIcon icon={TransitionTopIcon} className="size-4" />
          <span>{t("Export")}</span>
        </button>
      </DialogTrigger>
      {hasProject && (
        <ExportDialog
          open={isExportPopoverOpen}
          onOpenChange={setIsExportPopoverOpen}
        />
      )}
    </Dialog>
  );
}

/** File.name 常常没有后缀（就是 media id），扩展名按 MIME 猜，兜底 mp4。 */
function guessAssetExtension(file: File): string {
  const type = (file.type || "").toLowerCase();
  if (type.includes("mp4")) return "mp4";
  if (type.includes("webm")) return "webm";
  if (type.includes("quicktime")) return "mov";
  if (type.includes("wav")) return "wav";
  if (type.includes("mpeg")) return "mp3";
  if (type.includes("aac")) return "aac";
  const fromName = (file.name.split(".").pop() ?? "").toLowerCase();
  return /^[a-z0-9]{2,5}$/.test(fromName) ? fromName : "mp4";
}

const uploadedAssetIds = new Set<string>();

/** 把时间线交给 ComfyUI 侧 ffmpeg 渲染（默认路径，失败返回 null 走浏览器回退）。 */
async function renderTimelineOnServer({
  editor,
  canvasSize,
  fps,
  onProgress,
  isCancelled,
  resolution,
}: {
  editor: ReturnType<typeof useEditor>;
  canvasSize: { width: number; height: number };
  fps: number;
  onProgress: (value: number) => void;
  isCancelled: () => boolean;
  resolution: ExportResolution;
}): Promise<Blob> {
  try {
    const tracks = editor.timeline.getTracks();
    const assets = editor.media.getAssets();
    const duration = editor.timeline.getTotalDuration();
    if (!duration || duration <= 0) {
      throw new Error("项目时长为 0，没有可渲染内容");
    }

    const serverTracks: { elements: Record<string, unknown>[] }[] = [];

    for (const track of tracks) {
      const elements: Record<string, unknown>[] = [];

      for (const element of track.elements) {
        const start = element.startTime ?? 0;
        const length = element.duration ?? 0;
        if (length <= 0) continue;

        // 字幕/文字：不只看 type，任何"有文字内容且没有素材"的元素都按文字渲染，
        // 避免不同版本里字幕类型命名不一致导致漏掉（这是导出没有字幕的根因之一）
        const asText = element as unknown as {
          content?: string;
          fontSize?: number;
          color?: string;
          hidden?: boolean;
          mediaId?: string;
          transform?: { position?: { x: number; y: number } };
        };

        const looksLikeText =
          element.type === "text" ||
          (typeof asText.content === "string" &&
            asText.content.trim().length > 0 &&
            !asText.mediaId);

        if (looksLikeText) {
          if (asText.hidden || !asText.content?.trim()) continue;
          elements.push({
            type: "text",
            content: asText.content,
            start,
            duration: length,
            fontSize: Math.round(asText.fontSize ?? 48),
            color: asText.color ?? "#ffffff",
            x: Math.round(asText.transform?.position?.x ?? 0),
            y: Math.round(asText.transform?.position?.y ?? 0),
          });
          continue;
        }

        const mediaId = (element as { mediaId?: string }).mediaId;
        if (!mediaId) continue;

        const media = assets.find((item) => item.id === mediaId);
        if (!media?.file) continue;

        if (!uploadedAssetIds.has(mediaId)) {
          const ext = guessAssetExtension(media.file);
          const ok = await uploadAssetBlob({
            assetId: mediaId,
            blob: media.file,
            ext,
          });
          if (!ok) {
            throw new Error(`素材上传失败：${media.file.name}`);
          }
          uploadedAssetIds.add(mediaId);
        }

        if (element.type === "video" || element.type === "image") {
          const visual = element as unknown as {
            hidden?: boolean;
            muted?: boolean;
            opacity?: number;
            trimStart?: number;
            transform?: { position?: { x: number; y: number }; scale?: number };
          };
          if (visual.hidden) continue;
          // 人声美化：视频换声音但不换画面 —— 画面这条静音，另加一条处理后的音频
          // 分离音频（muted）后画面自己就没有声音了，不能再叠加一条处理后的音轨
          const beautify = element as unknown as {
            voiceBeautifyEnabled?: boolean;
            voiceBeautifyMediaId?: string;
            playbackRate?: number;
          };
          const beautifyMediaId =
            !visual.muted &&
            beautify.voiceBeautifyEnabled &&
            beautify.voiceBeautifyMediaId
              ? beautify.voiceBeautifyMediaId
              : "";
          if (beautifyMediaId && !uploadedAssetIds.has(beautifyMediaId)) {
            const beautyAsset = assets.find((item) => item.id === beautifyMediaId);
            if (!beautyAsset?.file) {
              throw new Error(`人声美化素材缺失：${beautifyMediaId}`);
            }
            const ok = await uploadAssetBlob({
              assetId: beautifyMediaId,
              blob: beautyAsset.file,
              ext: guessAssetExtension(beautyAsset.file),
            });
            if (!ok) {
              throw new Error(`素材上传失败：${beautyAsset.file.name}`);
            }
            uploadedAssetIds.add(beautifyMediaId);
          }
          elements.push({
            type: element.type,
            assetId: mediaId,
            muted: beautifyMediaId || visual.muted ? true : undefined,
            start,
            duration: length,
            trimStart: visual.trimStart ?? 0,
            scale: visual.transform?.scale ?? 1,
            x: Math.round(visual.transform?.position?.x ?? 0),
            y: Math.round(visual.transform?.position?.y ?? 0),
            opacity: visual.opacity ?? 1,
          });
          if (beautifyMediaId) {
            elements.push({
              type: "audio",
              assetId: beautifyMediaId,
              start,
              duration: length,
              trimStart: visual.trimStart ?? 0,
              volume: 1,
              playbackRate: beautify.playbackRate ?? 1,
            });
          }
        } else if (element.type === "audio") {
          const audio = element as unknown as {
            hidden?: boolean;
            muted?: boolean;
            volume?: number;
            trimStart?: number;
          };
          if (audio.hidden || audio.muted) continue;
          elements.push({
            type: "audio",
            assetId: mediaId,
            start,
            duration: length,
            trimStart: audio.trimStart ?? 0,
            volume: audio.volume ?? 1,
          });
        }
      }

      if (elements.length > 0) serverTracks.push({ elements });
    }

    if (serverTracks.length === 0) {
      throw new Error(
        "时间线里没有可渲染的素材（贴纸/转场暂不支持服务端渲染）",
      );
    }

    onProgress(0.02);
    const startResponse = await fetch(withBasePath("/api/render/start"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        canvas: canvasSize,
        output: resolveExportSize({ canvasSize, resolution }),
        fps,
        duration,
        profile: { crf: 19, preset: "veryfast", threads: 0 },
        tracks: serverTracks,
      }),
    });
    if (!startResponse.ok) {
      const detail = (await startResponse.text()).slice(0, 400);
      throw new Error(
        `服务端渲染启动失败 HTTP ${startResponse.status}：${detail}`,
      );
    }

    const { jobId } = (await startResponse.json()) as { jobId?: string };
    if (!jobId) {
      throw new Error("服务端没有返回任务 id");
    }

    let state = "running";
    let failedPolls = 0;

    for (let attempt = 0; attempt < 7200; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      const progressResponse = await fetch(
        `${withBasePath("/api/render/progress")}?jobId=${encodeURIComponent(jobId)}`,
      );

      if (progressResponse.status === 404) {
        throw new Error("服务端渲染任务已丢失（ComfyUI 可能重启过），请重新导出");
      }
      if (!progressResponse.ok) {
        failedPolls += 1;
        if (failedPolls >= 10) {
          throw new Error(
            `服务端进度接口连续失败（HTTP ${progressResponse.status}），请重试`,
          );
        }
        continue;
      }
      failedPolls = 0;

      if (isCancelled()) {
        await fetch(
          `${withBasePath("/api/render/cancel")}?jobId=${encodeURIComponent(jobId)}`,
          { method: "POST" },
        ).catch(() => undefined);
        throw new Error("已取消导出");
      }

      const info = (await progressResponse.json()) as {
        state?: string;
        progress?: number;
        error?: string | null;
      };
      state = info.state ?? "running";

      // 上传占 2%~10%，渲染占 10%~98%
      onProgress(Math.min(0.98, 0.1 + (info.progress ?? 0) * 0.88));

      if (state === "error") {
        throw new Error(`服务端渲染失败：${info.error ?? "未知错误"}`);
      }
      if (state === "done") break;
    }

    if (state !== "done") {
      throw new Error("服务端渲染超时");
    }

    const resultResponse = await fetch(
      `${withBasePath("/api/render/result")}?jobId=${encodeURIComponent(jobId)}`,
    );
    if (!resultResponse.ok) {
      const detail = (await resultResponse.text()).slice(0, 400);
      throw new Error(
        `取回渲染结果失败 HTTP ${resultResponse.status}：${detail}`,
      );
    }

    const blob = await resultResponse.blob();
    if (blob.size === 0) {
      throw new Error("服务端返回了空文件");
    }

    onProgress(1);
    return blob;
  } catch (error) {
    console.warn("[YuanCut] 服务端渲染失败：", error);
    throw error instanceof Error ? error : new Error(String(error));
  }
}

function ExportDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t: translate } = useTranslation();
  const t = useChineseLabels({ translate });
  const editor = useEditor();
  const activeProject = editor.project.getActive();
  const canvasSize = activeProject?.settings.canvasSize ?? {
    width: 1920,
    height: 1080,
  };

  const [exportVideo, setExportVideo] = useState(true);
  const [exportAudio, setExportAudio] = useState(false);
  const [exportSubtitle, setExportSubtitle] = useState(false);

  const [baseName, setBaseName] = useState(
    activeProject?.metadata.name ?? "untitled",
  );
  const directoryRef = useRef<DirectoryHandleLike | null>(null);
  const [directoryName, setDirectoryName] = useState("");
  const [serverDir, setServerDir] = useState("");

  useEffect(() => {
    let cancelled = false;
    void getServerExportDir().then((path) => {
      if (!cancelled) setServerDir(path ?? "");
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const [format, setFormat] = useState<ExportFormat>("mp4");
  const [resolution, setResolution] = useState<ExportResolution>("project");
  const [fps, setFps] = useState<number>(
    Math.round(activeProject?.settings.fps ?? 30),
  );
  const [videoCodec, setVideoCodec] = useState<ExportVideoCodec>("avc");
  const [bitrateMbps, setBitrateMbps] = useState<number>(0);
  const [hardwareEncode, setHardwareEncode] = useState<boolean>(false);
  const [availableCodecs, setAvailableCodecs] = useState<ExportVideoCodec[]>([
    "avc",
  ]);

  // 按容器 + 浏览器实际能力过滤编码器（avc 就是 H.264）
  useEffect(() => {
    let cancelled = false;
    const size = resolveExportSize({ canvasSize, resolution });

    void getAvailableVideoCodecs({
      format,
      width: size.width,
      height: size.height,
    }).then((codecs) => {
      if (cancelled) return;
      setAvailableCodecs(codecs);
      setVideoCodec((current) =>
        codecs.includes(current) ? current : (codecs[0] ?? "avc"),
      );
    });

    return () => {
      cancelled = true;
    };
  }, [format, resolution, canvasSize.width, canvasSize.height]);

  const [audioFormat, setAudioFormat] = useState<ExportAudioFormat>("wav");
  const [subtitleFormat, setSubtitleFormat] =
    useState<ExportSubtitleFormat>("srt");

  const [isExporting, setIsExporting] = useState(false);
  const [savedPaths, setSavedPaths] = useState<string[]>([]);

  // 挂载时兜底复位：避免上一次异常中断把 isExporting 留在 true
  useEffect(() => {
    setIsExporting(false);
    cancelRequestedRef.current = false;
  }, []);

  // 打开导出面板就暂停时间轴播放；并清空上一次的结果面板
  useEffect(() => {
    if (!open) return;

    try {
      editor.playback.pause();
    } catch {
      // 播放器可能尚未初始化，忽略
    }

    setExportResult(null);
    setSavedPaths([]);
    setProgress(0);
    setIsExporting(false);          // 上次异常中断可能留下 true
    cancelRequestedRef.current = false;
  }, [editor, open]);
  const [progress, setProgress] = useState(0);
  const [exportResult, setExportResult] = useState<ExportResult | null>(null);
  const cancelRequestedRef = useRef(false);

  const pickDirectory = async () => {
    const picker = (
      window as unknown as {
        showDirectoryPicker?: () => Promise<DirectoryHandleLike>;
      }
    ).showDirectoryPicker;

    if (typeof picker !== "function") {
      setDirectoryName(t("Not supported"));
      return;
    }

    try {
      const handle = await picker();
      directoryRef.current = handle;
      setDirectoryName(handle.name ?? "");
    } catch {
      // 用户取消，保持原样
    }
  };

  const writeFiles = async (files: ExportFile[]) => {
    const directory = directoryRef.current;

    if (directory) {
      try {
        for (const file of files) {
          const handle = await directory.getFileHandle(file.name, {
            create: true,
          });
          const writable = await handle.createWritable();
          await writable.write(file.blob);
          await writable.close();
        }
        return files.map(
          (file) => `${directory.name ?? ""}/${file.name}`,
        );
      } catch (error) {
        console.warn("[YuanCut] 写入所选目录失败，改为服务端保存：", error);
      }
    }

    // 首选：直接写进 ComfyUI 侧的导出目录（面板上显示的就是这个绝对路径）
    if (serverDir) {
      const saved: string[] = [];
      for (const file of files) {
        const path = await saveBlobToServer({
          blob: file.blob,
          fileName: file.name,
        });
        if (path) saved.push(path);
      }

      if (saved.length === files.length) {
        console.info("[YuanCut] 导出文件已保存：", saved.join("  "));
        return saved;
      }

      console.warn("[YuanCut] 部分文件保存失败，改为逐个下载");
    }

    const downloaded: string[] = [];
    for (const file of files) {
      const result = await saveExportBlob({
        blob: file.blob,
        fileName: file.name,
        mimeType: file.blob.type || "application/octet-stream",
      });
      if (result !== "cancelled") {
        downloaded.push(file.name);
      }
    }
    return downloaded;
  };

  const handleExport = async () => {
    if (!activeProject) return;

    if (!exportVideo && !exportAudio && !exportSubtitle) {
      setExportResult({ success: false, error: "至少勾选一项导出内容" });
      return;
    }

    cancelRequestedRef.current = false;
    setIsExporting(true);
    setProgress(0);
    setExportResult(null);

    try {
      const name = baseName.trim() || activeProject.metadata.name;
      const files: ExportFile[] = [];
      let audioBuffer: AudioBuffer | undefined;

      if (exportVideo) {
        // 默认路径：交给 ComfyUI 侧的 ffmpeg 渲染（失败才回退浏览器导出）
        const serverBlob = await renderTimelineOnServer({
          editor,
          canvasSize,
          fps,
          onProgress: setProgress,
          isCancelled: () => cancelRequestedRef.current,
          resolution,
        });

        if (!serverBlob) {
          throw new Error(
            "服务端渲染失败：请查看 ComfyUI 控制台日志（不再回退浏览器导出）",
          );
        }

        files.push({ name: `${name}.mp4`, blob: serverBlob });
      }

      if (exportAudio) {
        const buffer =
          audioBuffer ??
          (await createTimelineAudioBuffer({
            tracks: editor.timeline.getTracks(),
            mediaAssets: editor.media.getAssets(),
            duration: editor.timeline.getTotalDuration(),
          }));

        if (buffer) {
          const wav = encodeAudioBufferToWav(buffer);
          if (audioFormat === "wav") {
            files.push({ name: `${name}.wav`, blob: wav });
          } else {
            const converted = await convertAudioWithServer({
              audioBlob: wav,
              format: audioFormat,
            });
            files.push({
              name: `${name}.${audioFormat}`,
              blob: converted ?? wav,
            });
          }
        }
      }

      if (exportSubtitle) {
        const content = buildSubtitleFile({
          tracks: editor.timeline.getTracks(),
          format: subtitleFormat,
        });
        files.push({
          name: `${name}.${subtitleFormat}`,
          blob: new Blob([content], { type: "text/plain;charset=utf-8" }),
        });
      }

      if (files.length === 0) {
        setExportResult({ success: false, error: "没有可导出的内容" });
        setIsExporting(false);
        return;
      }

      const paths = await writeFiles(files);
      setSavedPaths(paths);
      setIsExporting(false);
      setProgress(0);
      setExportResult({ success: true });
    } catch (error) {
      setIsExporting(false);
      setExportResult({
        success: false,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  };

  const handleCancel = () => {
    cancelRequestedRef.current = true;
  };

  const targetLabel = [
    exportVideo ? `${baseName || "untitled"}${getExportFileExtension({ format })}` : null,
    exportAudio ? `${baseName || "untitled"}.${audioFormat}` : null,
    exportSubtitle ? `${baseName || "untitled"}.${subtitleFormat}` : null,
  ]
    .filter(Boolean)
    .join("  ·  ");

  return (
    <DialogContent className="max-h-[85vh] w-[min(560px,92vw)] gap-0 overflow-hidden p-0">
      {exportResult && !exportResult.success ? (
        <ExportError
          error={exportResult.error || "Unknown error occurred"}
          onRetry={handleExport}
          onClose={() => onOpenChange(false)}
        />
      ) : exportResult?.success ? (
        <ExportSuccess
          paths={savedPaths}
          outputDir={directoryName || serverDir}
          onClose={() => onOpenChange(false)}
        />
      ) : (
        <>
          <DialogHeader className="border-b p-3 pr-10">
            <DialogTitle className="text-sm font-medium">
              {isExporting ? t("Exporting project") : t("Export project")}
            </DialogTitle>
          </DialogHeader>

          <div
            className={cn(
              "flex max-h-[62vh] flex-col gap-3 overflow-y-auto p-3",
              isExporting && "opacity-40",
            )}
          >
            <div className="flex flex-col gap-1.5">
              <Label className="text-xs text-muted-foreground">
                {t("File name")}
              </Label>
              <Input
                value={baseName}
                onChange={(event) => setBaseName(event.target.value)}
                className="h-8"
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <Label className="text-xs text-muted-foreground">
                {t("Export folder")}
              </Label>
              <div className="flex items-center gap-2">
                <Input
                  readOnly
                  value={directoryName || serverDir}
                  placeholder={t("Browser default download folder")}
                  title={directoryName || serverDir}
                  className="h-8 text-xs"
                />
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-8 shrink-0 gap-1"
                  onClick={pickDirectory}
                  title={t("Choose a folder in this browser instead")}
                >
                  <FolderOpen className="size-3.5" />
                  {t("Choose")}
                </Button>
              </div>
              <p className="text-[11px] leading-tight text-muted-foreground">
                {serverDir
                  ? t("Files are written to this folder on the ComfyUI host.")
                  : t("Browser default download folder")}
              </p>
            </div>

            <div className="flex flex-col gap-2 rounded-lg border p-2">
              <div className="flex items-center gap-2">
                <Checkbox
                  id="export-video"
                  checked={exportVideo}
                  onCheckedChange={(checked) => setExportVideo(!!checked)}
                />
                <Label htmlFor="export-video" className="text-sm font-medium">
                  {t("Video export")}
                </Label>
              </div>
              <div
                className={cn(
                  "flex flex-col gap-2 pl-6",
                  !exportVideo && "opacity-40",
                )}
              >
                <SettingRow label={t("Format")}>
                  <Select
                    value={format}
                    onValueChange={(value) => {
                      if (isExportFormat(value)) setFormat(value);
                    }}
                    disabled={!exportVideo}
                  >
                    <SelectTrigger className="h-7 w-36">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="z-[1200]">
                      {EXPORT_FORMAT_VALUES.map((item) => (
                        <SelectItem key={item} value={item}>
                          {item.toUpperCase()}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </SettingRow>
                <SettingRow label={t("Resolution")}>
                  <Select
                    value={resolution}
                    onValueChange={(value) => {
                      if (isExportResolution(value)) setResolution(value);
                    }}
                    disabled={!exportVideo}
                  >
                    <SelectTrigger className="h-7 w-36">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="z-[1200]">
                      {EXPORT_RESOLUTION_VALUES.map((item) => (
                        <SelectItem key={item} value={item}>
                          {getExportResolutionLabel({
                            resolution: item,
                            canvasSize,
                          })}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </SettingRow>
                <SettingRow label={t("Frame rate")}>
                  <Select
                    value={String(fps)}
                    onValueChange={(value) => setFps(Number.parseInt(value, 10))}
                    disabled={!exportVideo}
                  >
                    <SelectTrigger className="h-7 w-36">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="z-[1200]">
                      {EXPORT_FPS_VALUES.map((item) => (
                        <SelectItem key={item} value={String(item)}>
                          {`${item} fps`}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </SettingRow>
                <SettingRow label={t("Codec")}>
                  <Select
                    value={videoCodec}
                    onValueChange={(value) => {
                      if (isExportVideoCodec(value)) setVideoCodec(value);
                    }}
                    disabled={!exportVideo}
                  >
                    <SelectTrigger className="h-7 w-36">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="z-[1200]">
                      {availableCodecs.map((item) => (
                        <SelectItem key={item} value={item}>
                          {getCodecLabel(item)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </SettingRow>
                <SettingRow label={t("Bitrate")}>
                  <Select
                    value={String(bitrateMbps)}
                    onValueChange={(value) =>
                      setBitrateMbps(Number.parseInt(value, 10))
                    }
                    disabled={!exportVideo}
                  >
                    <SelectTrigger className="h-7 w-36">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="z-[1200]">
                      {EXPORT_BITRATE_VALUES.map((item) => (
                        <SelectItem key={item} value={String(item)}>
                          {item === 0 ? t("Auto") : `${item} Mbps`}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </SettingRow>
                <SettingRow label={t("Hardware encoding")}>
                  <Select
                    value={hardwareEncode ? "on" : "off"}
                    onValueChange={(value) => setHardwareEncode(value === "on")}
                    disabled={!exportVideo}
                  >
                    <SelectTrigger className="h-7 w-36">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="z-[1200]">
                      <SelectItem value="off">{t("Off (software)")}</SelectItem>
                      <SelectItem value="on">{t("On (hardware)")}</SelectItem>
                    </SelectContent>
                  </Select>
                </SettingRow>
              </div>
            </div>

            <div className="flex flex-col gap-2 rounded-lg border p-2">
              <div className="flex items-center gap-2">
                <Checkbox
                  id="export-audio"
                  checked={exportAudio}
                  onCheckedChange={(checked) => setExportAudio(!!checked)}
                />
                <Label htmlFor="export-audio" className="text-sm font-medium">
                  {t("Audio export")}
                </Label>
              </div>
              <div
                className={cn(
                  "flex flex-col gap-2 pl-6",
                  !exportAudio && "opacity-40",
                )}
              >
                <SettingRow label={t("Format")}>
                  <Select
                    value={audioFormat}
                    onValueChange={(value) => {
                      if (isExportAudioFormat(value)) setAudioFormat(value);
                    }}
                    disabled={!exportAudio}
                  >
                    <SelectTrigger className="h-7 w-36">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="z-[1200]">
                      {EXPORT_AUDIO_FORMAT_VALUES.map((item) => (
                        <SelectItem key={item} value={item}>
                          {item.toUpperCase()}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </SettingRow>
              </div>
            </div>

            <div className="flex flex-col gap-2 rounded-lg border p-2">
              <div className="flex items-center gap-2">
                <Checkbox
                  id="export-subtitle"
                  checked={exportSubtitle}
                  onCheckedChange={(checked) => setExportSubtitle(!!checked)}
                />
                <Label
                  htmlFor="export-subtitle"
                  className="text-sm font-medium"
                >
                  {t("Subtitle export")}
                </Label>
              </div>
              <div
                className={cn(
                  "flex flex-col gap-2 pl-6",
                  !exportSubtitle && "opacity-40",
                )}
              >
                <SettingRow label={t("Format")}>
                  <Select
                    value={subtitleFormat}
                    onValueChange={(value) => {
                      if (isExportSubtitleFormat(value)) {
                        setSubtitleFormat(value);
                      }
                    }}
                    disabled={!exportSubtitle}
                  >
                    <SelectTrigger className="h-7 w-36">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="z-[1200]">
                      {EXPORT_SUBTITLE_FORMAT_VALUES.map((item) => (
                        <SelectItem key={item} value={item}>
                          {item.toUpperCase()}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </SettingRow>
              </div>
            </div>

            {targetLabel ? (
              <p className="text-xs text-muted-foreground break-all">
                {targetLabel}
              </p>
            ) : null}
          </div>

          {isExporting ? (
            <div className="space-y-3 p-3 pt-0">
              <p className="text-muted-foreground text-sm">
                {Math.round(progress * 100)}%
              </p>
              <Progress value={progress * 100} className="w-full" />
              <Button
                variant="outline"
                className="w-full rounded-md"
                onClick={handleCancel}
              >
                {t("Cancel")}
              </Button>
            </div>
          ) : (
            <div className="p-3 pt-0">
              <Button onClick={handleExport} className="w-full gap-2">
                <Download className="size-4" />
                {t("Export")}
              </Button>
            </div>
          )}
        </>
      )}
    </DialogContent>
  );
}

function SettingRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-xs text-muted-foreground">{label}</span>
      {children}
    </div>
  );
}

function ExportSuccess({
  paths,
  outputDir,
  onClose,
}: {
  paths: string[];
  outputDir: string;
  onClose: () => void;
}) {
  const { t: translate } = useTranslation();
  const t = useChineseLabels({ translate });

  return (
    <div className="flex flex-col gap-3 p-4">
      <h3 className="text-sm font-medium text-emerald-600">
        {t("Exported successfully")}
      </h3>
      {outputDir ? (
        <p className="text-xs text-muted-foreground break-all">
          {t("Output folder")}: {outputDir}
        </p>
      ) : null}
      <ul className="flex flex-col gap-1 text-xs break-all">
        {paths.length > 0 ? (
          paths.map((path) => <li key={path}>· {path}</li>)
        ) : (
          <li>· {t("File saved to the browser download folder")}</li>
        )}
      </ul>
      <Button variant="outline" className="w-full" onClick={onClose}>
        {t("Close")}
      </Button>
    </div>
  );
}

function ExportError({
  error,
  onRetry,
  onClose,
}: {
  error: string;
  onRetry: () => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();

  return (
    <div className="flex flex-col gap-3 p-3">
      <h3 className="text-sm font-medium">{t("Export failed")}</h3>
      <p className="text-destructive text-xs whitespace-pre-wrap break-all">
        {error}
      </p>
      <div className="flex gap-2">
        <Button variant="outline" className="flex-1 gap-2" onClick={onRetry}>
          <RotateCcw className="size-4" />
          {t("Retry")}
        </Button>
        <Button variant="outline" className="flex-1" onClick={onClose}>
          {t("Close")}
        </Button>
      </div>
    </div>
  );
}

function isExportFormat(value: string): value is ExportFormat {
  return EXPORT_FORMAT_VALUES.some((item) => item === value);
}

function isExportAudioFormat(value: string): value is ExportAudioFormat {
  return EXPORT_AUDIO_FORMAT_VALUES.some((item) => item === value);
}

function isExportSubtitleFormat(value: string): value is ExportSubtitleFormat {
  return EXPORT_SUBTITLE_FORMAT_VALUES.some((item) => item === value);
}
""")

write(EXPORT_BUTTON, EXPORT_BUTTON_SOURCE + "\n")
print("  wrote   export-button.tsx（三段式导出面板）")

print("  patched 三段式导出面板（视频 / 音频 / 字幕）")

# ------------------------------------------------ 字幕转录改用 MOSS-Transcribe-Diarize
# 原来字幕转录在浏览器里跑 Whisper（Small / Large v3 Turbo / Distil Small）三个模型。
# 现在三个模型全部下线：字幕转录统一调用 ComfyUI 本机的 MOSS-Transcribe-Diarize，
# 前端把 16 kHz float32 波形 POST 到 /yuancut/transcribe，后端返回带说话人的句段。
write(
    SRC / "constants" / "transcription-constants.ts",
    '''import { LANGUAGES } from "@/constants/language-constants";
import type {
	TranscriptionModel,
	TranscriptionModelId,
} from "@/types/transcription";
import type { LanguageCode } from "@/types/language";

const SUPPORTED_TRANSCRIPTION_LANGS: ReadonlyArray<LanguageCode> = [
	"en",
	"es",
	"it",
	"fr",
	"de",
	"pt",
	"ru",
	"ja",
	"zh",
];

export const TRANSCRIPTION_LANGUAGES = LANGUAGES.filter((language) =>
	SUPPORTED_TRANSCRIPTION_LANGS.includes(language.code),
);

// 字幕转录只保留一种方法：ComfyUI 本机的 MOSS-Transcribe-Diarize。
// 原来的 Whisper Small / Large v3 Turbo / Distil Small 三个模型已全部删除。
export const TRANSCRIPTION_MODELS: TranscriptionModel[] = [
	{
		id: "moss-transcribe-diarize",
		name: "MOSS-Transcribe-Diarize",
		huggingFaceId: "",
		description: "本机 MOSS 转写 + 说话人分离（服务端推理，无需下载权重）",
		encoderDtype: "fp32",
	},
];

export const DEFAULT_TRANSCRIPTION_MODEL: TranscriptionModelId =
	"moss-transcribe-diarize";

export const DEFAULT_CHUNK_LENGTH_SECONDS = 30;
export const DEFAULT_STRIDE_SECONDS = 5;

export const DEFAULT_WORDS_PER_CAPTION = 3;
export const MIN_CAPTION_DURATION_SECONDS = 0.8;
''',
)
print("  wrote   transcription-constants.ts（只保留 MOSS-Transcribe-Diarize）")

p = SRC / "types" / "transcription.ts"
patch(
    p,
    '''export type TranscriptionModelId =
	| "whisper-tiny"
	| "whisper-base"
	| "whisper-small"
	| "whisper-large-v3-turbo"
	| "distil-small.en";''',
    '''// 字幕转录只保留 MOSS-Transcribe-Diarize 一种方法；
// 原先的 Whisper Tiny / Base / Small / Large v3 Turbo / Distil Small 已删除
export type TranscriptionModelId = "moss-transcribe-diarize";''',
)

write(
    SRC / "services" / "transcription" / "worker.ts",
    '''import type {
	TranscriptionChunk,
	TranscriptionSegment,
} from "@/types/transcription";

// 这里只保留消息类型；真正的 worker 运行时在 worker-runtime.js。
export type WorkerMessage =
	| {
			type: "init";
			modelId: string;
			encoderDtype: "fp16" | "fp32";
	  }
	| {
			type: "transcribe";
			audio: Float32Array;
			language: string;
			subtask: string | null;
	  }
	| { type: "cancel" };

export type WorkerResponse =
	| { type: "init-progress"; progress: number }
	| { type: "init-complete" }
	| { type: "init-error"; error: string }
	| {
			type: "transcribe-update";
			chunks: TranscriptionChunk[];
			tps: number;
	  }
	| { type: "transcribe-progress"; progress: number }
	| {
			type: "transcribe-complete";
			text: string;
			segments: TranscriptionSegment[];
			tps: number;
	  }
	| { type: "transcribe-error"; error: string }
	| { type: "cancelled" };
''',
)

# 必须是纯 JS：Next 会把 `new URL(...)` 指向的文件当静态资源原样输出。
# 之前 worker.ts 被当成 .ts 资源（MIME 是 video/mp2t）发给浏览器，模块 worker
# 根本不会执行；改成 .js 后按 text/javascript 送达。
write(
    SRC / "services" / "transcription" / "worker-runtime.js",
    '''// 字幕转录统一走 ComfyUI 本机的 MOSS-Transcribe-Diarize（见 yuancut_transcribe.py）。
const TRANSCRIBE_ENDPOINT = "/yuancut/transcribe";
const SAMPLE_RATE = 16000;
// 单次请求最多送 5 分钟音频，更长的音频分块发送后按时间偏移拼接
const MAX_WINDOW_SECONDS = 300;

let cancelled = false;
let activeController = null;

self.onmessage = async (event) => {
	const message = event.data;

	switch (message.type) {
		case "init":
			// 模型常驻 ComfyUI 服务端，前端不需要下载权重
			self.postMessage({ type: "init-progress", progress: 100 });
			self.postMessage({ type: "init-complete" });
			break;
		case "transcribe":
			await handleTranscribe({
				audio: message.audio,
				language: message.language,
			});
			break;
		case "cancel":
			cancelled = true;
			if (activeController) activeController.abort();
			self.postMessage({ type: "cancelled" });
			break;
	}
};

function toBase64(samples) {
	const bytes = new Uint8Array(
		samples.buffer,
		samples.byteOffset,
		samples.byteLength,
	);
	const BLOCK = 0x8000;
	let binary = "";
	for (let index = 0; index < bytes.length; index += BLOCK) {
		const slice = bytes.subarray(index, index + BLOCK);
		binary += String.fromCharCode(...slice);
	}
	return btoa(binary);
}

async function handleTranscribe({ audio, language }) {
	cancelled = false;
	const startedAt = performance.now();
	const windowSamples = Math.floor(MAX_WINDOW_SECONDS * SAMPLE_RATE);
	const windowCount = Math.max(1, Math.ceil(audio.length / windowSamples));

	const segments = [];
	const chunks = [];
	let collectedText = "";
	let tps = 0;

	try {
		for (let windowIndex = 0; windowIndex < windowCount; windowIndex++) {
			if (cancelled) return;

			const startSample = windowIndex * windowSamples;
			const endSample = Math.min(audio.length, startSample + windowSamples);
			if (endSample - startSample < SAMPLE_RATE * 0.2) continue;

			const offsetS = startSample / SAMPLE_RATE;
			const windowAudio = audio.slice(startSample, endSample);

			activeController = new AbortController();
			const response = await fetch(TRANSCRIBE_ENDPOINT, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					audio: toBase64(windowAudio),
					sample_rate: SAMPLE_RATE,
					language,
				}),
				signal: activeController.signal,
			});
			activeController = null;

			if (!response.ok) {
				let detail = "MOSS 转写失败：HTTP " + response.status;
				try {
					const payload = await response.json();
					if (payload && payload.error) detail = payload.error;
				} catch {
					// 保留 HTTP 状态码
				}
				throw new Error(detail);
			}

			const payload = await response.json();
			for (const item of payload.segments || []) {
				const text = (item.text || "").trim();
				if (!text) continue;
				const start = offsetS + Number(item.start || 0);
				const end = offsetS + Number(item.end || item.start || 0);
				segments.push({ text, start, end });
				chunks.push({
					text,
					timestamp: [start, end],
					finalised: true,
					offset: offsetS,
				});
			}
			if (payload.text) {
				collectedText = [collectedText, payload.text.trim()]
					.filter((part) => part.length > 0)
					.join(" ");
			}

			const elapsedSeconds = (performance.now() - startedAt) / 1000;
			tps = elapsedSeconds > 0 ? collectedText.length / elapsedSeconds : 0;

			self.postMessage({ type: "transcribe-update", chunks, tps });
			self.postMessage({
				type: "transcribe-progress",
				progress: Math.min(
					99,
					Math.round(((windowIndex + 1) / windowCount) * 100),
				),
			});
		}

		self.postMessage({
			type: "transcribe-complete",
			text: collectedText,
			segments,
			tps,
		});
	} catch (error) {
		if (cancelled) return;
		self.postMessage({
			type: "transcribe-error",
			error: error instanceof Error ? error.message : "MOSS 转写失败",
		});
	} finally {
		activeController = null;
	}
}
''',
)

p = SRC / "services" / "transcription" / "service.ts"
patch(
    p,
    'new Worker(new URL("./worker.ts", import.meta.url), {',
    'new Worker(new URL("./worker-runtime.js", import.meta.url), {',
)

print("  wrote   worker.ts / worker-runtime.js（字幕转录改调 MOSS-Transcribe-Diarize）")

# 模型下拉框换成只读的模型名，界面上不再出现三个 Whisper 选项
p = SRC / "components" / "editor" / "panels" / "assets" / "views" / "captions.tsx"
text = p.read_text(encoding="utf-8")
model_select_re = re.compile(
    r'<Label>\{t\("Model"\)\}</Label>\s*<Select\s+value=\{selectedModelId\}.*?</Select>',
    re.S,
)
model_block = '''<Label>{t("Model")}</Label>
							<div className="flex items-center rounded-md border px-3 py-2">
								<span className="text-sm">
									{TRANSCRIPTION_MODELS.find((m) => m.id === selectedModelId)
										?.name ?? "MOSS-Transcribe-Diarize"}
								</span>
							</div>'''
text, removed = model_select_re.subn(lambda _match: model_block, text, count=1)
if removed != 1:
    raise SystemExit("captions.tsx 的模型下拉锚点未找到")
p.write_text(text, encoding="utf-8")
print("  patched captions.tsx（去掉模型下拉，只显示 MOSS-Transcribe-Diarize）")

print("  patched 字幕转录统一使用 MOSS-Transcribe-Diarize")

# ------------------------------------------------ 字幕底色贴合文字长度
# 转录出来的字幕被写死了 boxWidth（画布宽 90%），换行渲染时底色直接铺满整个排版框，
# 于是短句也会拖出一条很长的黑底。这里改成按「最宽一行的实测宽度」画底色：
# 文字不变，只是黑底不再超出文字。
p = SRC / "services" / "renderer" / "nodes" / "text-node.ts"
patch(
    p,
    '''			context.fillStyle = this.params.backgroundColor;
			const bgX = -scaledBoxWidth / 2 - padX;
			const bgY = startY - lineHeight / 2 - padY;
			const bgW = scaledBoxWidth + padX * 2;
			const bgH = totalHeight + padY * 2;''',
    '''			context.fillStyle = this.params.backgroundColor;
			// 底色只包住文字本身：按最宽一行的实测宽度计算，不再铺满整个排版框。
			// 文字仍然按 scaledBoxWidth 换行，位置和字号都不受影响。
			let widestLineWidth = 0;
			for (const line of lines) {
				widestLineWidth = Math.max(
					widestLineWidth,
					context.measureText(line).width,
				);
			}
			const bgW = widestLineWidth + padX * 2;
			let bgX = -bgW / 2;
			if (context.textAlign === "left") {
				bgX = textX - padX;
			} else if (context.textAlign === "right") {
				bgX = textX + padX - bgW;
			}
			const bgY = startY - lineHeight / 2 - padY;
			const bgH = totalHeight + padY * 2;''',
)
print("  patched text-node.ts（字幕黑底按文字宽度对齐）")

# ------------------------------------------------ 素材框选 + 删除素材不毁时间轴
# 1) 删素材库里的文件时不再连带删除时间轴片段：片段保留原位与时长，
#    时间轴上标红显示「素材丢失」，用户可以自己删或重新导入。
# 2) 媒体 / 音频面板支持框选（在空白处拖出选框），选中后批量删除。
write(
    SRC / "stores" / "asset-selection-store.ts",
    '''import { create } from "zustand";

interface AssetSelectionState {
	selectedIds: string[];
	setSelectedIds: ({ ids }: { ids: string[] }) => void;
	clear: () => void;
}

// 素材面板（媒体 / 音频）共用的框选结果
export const useAssetSelectionStore = create<AssetSelectionState>((set) => ({
	selectedIds: [],
	setSelectedIds: ({ ids }) => set({ selectedIds: ids }),
	clear: () => set({ selectedIds: [] }),
}));
''',
)

write(
    SRC / "hooks" / "use-asset-marquee.ts",
    '''"use client";

import { useCallback, useEffect, useState } from "react";
import { useAssetSelectionStore } from "@/stores/asset-selection-store";

export type MarqueePos = { x: number; y: number };

const ITEM_SELECTOR = "[data-asset-id]";
// 少于这个像素就当点击，不算框选
const MIN_DRAG_SIZE = 3;

/**
 * 素材面板框选：在空白处按下并拖出选框，选中与选框相交的素材。
 * 选中结果同时写进 DOM 的 data-selected，列表用 CSS 高亮即可。
 */
export function useAssetMarquee({
	containerRef,
}: {
	containerRef: React.RefObject<HTMLElement | null>;
}) {
	const setSelectedIds = useAssetSelectionStore((state) => state.setSelectedIds);
	const selectedIds = useAssetSelectionStore((state) => state.selectedIds);
	const [active, setActive] = useState(false);
	const [startPos, setStartPos] = useState<MarqueePos | null>(null);
	const [currentPos, setCurrentPos] = useState<MarqueePos | null>(null);

	const applySelection = useCallback(
		({ ids }: { ids: string[] }) => {
			setSelectedIds({ ids });
		},
		[setSelectedIds],
	);

	const clearSelection = useCallback(() => {
		applySelection({ ids: [] });
	}, [applySelection]);

	/** 右键某个素材时：没选中就先只选中它 */
	const selectOnly = useCallback(
		({ id }: { id: string }) => {
			applySelection({ ids: [id] });
		},
		[applySelection],
	);

	// 切到别的标签页时清空选中，避免残留
	useEffect(() => {
		return () => applySelection({ ids: [] });
	}, [applySelection]);

	const handleMouseDown = useCallback((event: React.MouseEvent) => {
		if (event.button !== 0) return;
		const target = event.target as HTMLElement;
		// 点在素材上、按钮上或滚动条上时不启动框选
		if (target.closest(ITEM_SELECTOR) || target.closest("button")) return;
		event.preventDefault();
		setActive(true);
		setStartPos({ x: event.clientX, y: event.clientY });
		setCurrentPos({ x: event.clientX, y: event.clientY });
	}, []);

	useEffect(() => {
		if (!active) return;

		const handleMove = (event: MouseEvent) => {
			setCurrentPos({ x: event.clientX, y: event.clientY });
		};

		const handleUp = (event: MouseEvent) => {
			const container = containerRef.current;
			const start = startPos;
			if (container && start) {
				const left = Math.min(start.x, event.clientX);
				const right = Math.max(start.x, event.clientX);
				const top = Math.min(start.y, event.clientY);
				const bottom = Math.max(start.y, event.clientY);
				const isDrag = right - left > MIN_DRAG_SIZE || bottom - top > MIN_DRAG_SIZE;
				const ids = isDrag
					? Array.from(container.querySelectorAll<HTMLElement>(ITEM_SELECTOR))
							.filter((node) => {
								const box = node.getBoundingClientRect();
								return (
									box.left < right &&
									box.right > left &&
									box.top < bottom &&
									box.bottom > top
								);
							})
							.map((node) => node.dataset.assetId)
							.filter((id): id is string => Boolean(id))
					: [];
				applySelection({ ids });
			}
			setActive(false);
			setStartPos(null);
			setCurrentPos(null);
		};

		window.addEventListener("mousemove", handleMove);
		window.addEventListener("mouseup", handleUp);
		return () => {
			window.removeEventListener("mousemove", handleMove);
			window.removeEventListener("mouseup", handleUp);
		};
	}, [active, startPos, containerRef, applySelection]);

	return {
		active,
		startPos,
		currentPos,
		selectedIds,
		clearSelection,
		selectOnly,
		handleMouseDown,
	};
}
''',
)

# 删素材时保留时间轴片段
p = SRC / "core" / "managers" / "media-manager.ts"
patch(
    p,
    '''\t\tconst tracks = this.editor.timeline.getTracks();
\t\tconst elementsToRemove: Array<{ trackId: string; elementId: string }> = [];

\t\tfor (const track of tracks) {
\t\t\tfor (const element of track.elements) {
\t\t\t\tif (hasMediaId(element) && element.mediaId === id) {
\t\t\t\t\telementsToRemove.push({ trackId: track.id, elementId: element.id });
\t\t\t\t}
\t\t\t}
\t\t}

\t\tif (elementsToRemove.length > 0) {
\t\t\tthis.editor.timeline.deleteElements({ elements: elementsToRemove });
\t\t}
''',
    '''\t\t// 只删素材库里的文件：时间轴上的片段保留原位与时长，
\t\t// 找不到素材的片段会在时间轴上标红显示「素材丢失」。
''',
)
patch_re(
    p,
    r'import \{ hasMediaId \} from "@/lib/timeline/element-utils";\n',
    '',
)

# 时间轴片段：素材丢失时标红
p = SRC / "components" / "editor" / "panels" / "timeline" / "timeline-element.tsx"
patch(
    p,
    '\tconst hasAudio = mediaSupportsAudio({ media: mediaAsset });',
    '\tconst hasAudio = mediaSupportsAudio({ media: mediaAsset });\n'
    "\t// 素材库里已经没有这段素材（被删了），但片段仍然留在时间轴上\n"
    "\tconst isMissingMedia = hasMediaId(element) && !mediaAsset;",
)
patch(
    p,
    '''\t\t\t\t\t\thandleResizeStart={handleResizeStart}
\t\t\t\t\t/>
\t\t\t\t</div>
\t\t\t</ContextMenuTrigger>''',
    '''\t\t\t\t\t\thandleResizeStart={handleResizeStart}
\t\t\t\t\t/>
\t\t\t\t\t{isMissingMedia && (
\t\t\t\t\t\t<div className="pointer-events-none absolute inset-0 z-20 flex items-center rounded-sm border-2 border-red-500 bg-red-500/25 px-1">
\t\t\t\t\t\t\t<span className="truncate text-[10px] font-medium text-white">
\t\t\t\t\t\t\t\t素材丢失
\t\t\t\t\t\t\t</span>
\t\t\t\t\t\t</div>
\t\t\t\t\t)}
\t\t\t\t</div>
\t\t\t</ContextMenuTrigger>''',
)

# 媒体面板：框选 + 批量删除
p = SRC / "components" / "editor" / "panels" / "assets" / "views" / "media.tsx"
patch(p, 'import { useMemo, useState } from "react";', 'import { useMemo, useRef, useState } from "react";')
patch(
    p,
    'import { useRevealItem } from "@/hooks/use-reveal-item";',
    'import { useRevealItem } from "@/hooks/use-reveal-item";\n'
    'import { useAssetMarquee } from "@/hooks/use-asset-marquee";\n'
    'import { SelectionBox } from "@/components/editor/selection-box";\n'
    'import { useAssetSelectionStore } from "@/stores/asset-selection-store";',
)
patch(
    p,
    '''\tconst handleClearSelection = () => {
\t\tuseMediaPreviewStore.getState().clearSelection();
\t};''',
    '''\tconst handleClearSelection = () => {
\t\tuseMediaPreviewStore.getState().clearSelection();
\t};

\tconst panelRef = useRef<HTMLDivElement>(null);
\tconst marquee = useAssetMarquee({ containerRef: panelRef });

\tconst handleRemoveSelected = async () => {
\t\tif (!activeProject || marquee.selectedIds.length === 0) return;
\t\tconst ids = [...marquee.selectedIds];
\t\tmarquee.clearSelection();
\t\thandleClearSelection();
\t\tfor (const id of ids) {
\t\t\tawait editor.media.removeMediaAsset({
\t\t\t\tprojectId: activeProject.metadata.id,
\t\t\t\tid,
\t\t\t});
\t\t}
\t};''',
)
patch(
    p,
    '''\t\t\t\t{...dragProps}
\t\t\t>''',
    '''\t\t\t\tref={panelRef}
\t\t\t\t{...dragProps}
\t\t\t>
\t\t\t\t<SelectionBox
\t\t\t\t\tstartPos={marquee.startPos}
\t\t\t\t\tcurrentPos={marquee.currentPos}
\t\t\t\t\tcontainerRef={panelRef}
\t\t\t\t\tisActive={marquee.active}
\t\t\t\t/>''',
)
patch(
    p,
    '''\t\t\t<div
\t\t\t\tclassName="scrollbar-thin size-full overflow-y-auto"
\t\t\t\tonClick={(event) => {
\t\t\t\t\tif (event.target === event.currentTarget) handleClearSelection();
\t\t\t\t}}
\t\t\t>''',
    '''\t\t\t<div
\t\t\t\tclassName="scrollbar-thin size-full overflow-y-auto"
\t\t\t\tonMouseDown={marquee.handleMouseDown}
\t\t\t\tonClick={(event) => {
\t\t\t\t\tif (event.target === event.currentTarget) handleClearSelection();
\t\t\t\t}}
\t\t\t>''',
)
patch_re(
    p,
    r"<div key=\{item\.id\} ref=\{\(el\) => registerElement\(item\.id, el\)\}>",
    '''<div
\t\t\t\t\tkey={item.id}
\t\t\t\t\tref={(el) => registerElement(item.id, el)}
\t\t\t\t\tdata-asset-id={item.id}
\t\t\t\t>''',
    expect=2,
)
patch(p, 'import { useMemo, useRef, useState } from "react";', 'import { useEffect, useMemo, useRef, useState } from "react";')

# 选中数量显示在面板右下角
patch(
    p,
    '''\t\t\t\t<SelectionBox
\t\t\t\t\tstartPos={marquee.startPos}
\t\t\t\t\tcurrentPos={marquee.currentPos}
\t\t\t\t\tcontainerRef={panelRef}
\t\t\t\t\tisActive={marquee.active}
\t\t\t\t/>''',
    '''\t\t\t\t<SelectionBox
\t\t\t\t\tstartPos={marquee.startPos}
\t\t\t\t\tcurrentPos={marquee.currentPos}
\t\t\t\t\tcontainerRef={panelRef}
\t\t\t\t\tisActive={marquee.active}
\t\t\t\t/>
\t\t\t\t{marquee.selectedIds.length > 0 && (
\t\t\t\t\t<div className="bg-foreground/85 text-background pointer-events-none absolute right-3 bottom-3 z-40 rounded px-2 py-1 text-[11px] shadow">
\t\t\t\t\t\t已选 {marquee.selectedIds.length} 项
\t\t\t\t\t</div>
\t\t\t\t)}''',
)

# 键盘 Delete / Backspace 删除所选
patch(
    p,
    '''\t\t\tawait editor.media.removeMediaAsset({
\t\t\t\tprojectId: activeProject.metadata.id,
\t\t\t\tid,
\t\t\t});
\t\t}
\t};''',
    '''\t\t\tawait editor.media.removeMediaAsset({
\t\t\t\tprojectId: activeProject.metadata.id,
\t\t\t\tid,
\t\t\t});
\t\t}
\t};

\t// 键盘 Delete / Backspace 删除选中的素材
\tuseEffect(() => {
\t\tif (marquee.selectedIds.length === 0) return;
\t\tconst onKeyDown = (event: KeyboardEvent) => {
\t\t\tif (event.key !== "Delete" && event.key !== "Backspace") return;
\t\t\tconst target = event.target as HTMLElement | null;
\t\t\tif (
\t\t\t\ttarget &&
\t\t\t\t(target.tagName === "INPUT" ||
\t\t\t\t\ttarget.tagName === "TEXTAREA" ||
\t\t\t\t\ttarget.isContentEditable)
\t\t\t) {
\t\t\t\treturn;
\t\t\t}
\t\t\tevent.preventDefault();
\t\t\tvoid handleRemoveSelected();
\t\t};
\t\twindow.addEventListener("keydown", onKeyDown);
\t\treturn () => window.removeEventListener("keydown", onKeyDown);
\t});''',
)
# 选中高亮 + 右键菜单里的「删除所选」
patch_re(
    p,
    r"isSelected=\{selectedMediaId === item\.id\}",
    "isSelected={selectedMediaId === item.id || selectedIds.includes(item.id)}",
    expect=2,
)
patch(
    p,
    '''\treturn (
\t\t<div
\t\t\tclassName="grid gap-1.5"''',
    '''\tconst selectedIds = useAssetSelectionStore((state) => state.selectedIds);

\treturn (
\t\t<div
\t\t\tclassName="grid gap-1.5"''',
)
patch(
    p,
    '''\treturn (
\t\t<div className="space-y-1">''',
    '''\tconst selectedIds = useAssetSelectionStore((state) => state.selectedIds);

\treturn (
\t\t<div className="space-y-1">''',
)
patch(
    p,
    '''\tonExportClip: ({ item }: { item: MediaAsset }) => void;
}) {
\tconst { t } = useTranslation();''',
    '''\tonExportClip: ({ item }: { item: MediaAsset }) => void;
}) {
\tconst { t } = useTranslation();
\tconst editor = useEditor();
\tconst activeProject = editor.project.getActive();
\tconst selectedIds = useAssetSelectionStore((state) => state.selectedIds);
\tconst clearAssetSelection = useAssetSelectionStore((state) => state.clear);

\tconst handleRemoveSelected = async () => {
\t\tif (!activeProject) return;
\t\tconst ids = [...selectedIds];
\t\tclearAssetSelection();
\t\tfor (const id of ids) {
\t\t\tawait editor.media.removeMediaAsset({
\t\t\t\tprojectId: activeProject.metadata.id,
\t\t\t\tid,
\t\t\t});
\t\t}
\t};''',
)
patch(
    p,
    '''\t\t\t<ContextMenuContent>
\t\t\t\t<ContextMenuItem onClick={() => onExportClip({ item })}>''',
    '''\t\t\t<ContextMenuContent>
\t\t\t\t{isMultiSelected && (
\t\t\t\t\t<>
\t\t\t\t\t\t<ContextMenuItem
\t\t\t\t\t\t\tvariant="destructive"
\t\t\t\t\t\t\tonClick={() => void handleRemoveSelected()}
\t\t\t\t\t\t>
\t\t\t\t\t\t\t删除所选 {selectedIds.length} 项
\t\t\t\t\t\t</ContextMenuItem>
\t\t\t\t\t\t<ContextMenuSeparator />
\t\t\t\t\t</>
\t\t\t\t)}
\t\t\t\t<ContextMenuItem onClick={() => onExportClip({ item })}>''',
)
# ------------------------------------------------ 媒体 / 音频分成两套素材库
# 素材多一个 panel 标记：media = 媒体面板，audio = 音频面板。
# 媒体里导入的音频留在媒体，音频面板导入/提取的素材也不会出现在媒体。
patch(
    SRC / "services" / "storage" / "types.ts",
    '''\tephemeral?: boolean;
\tthumbnailUrl?: string;
}''',
    '''\tephemeral?: boolean;
\tthumbnailUrl?: string;
\t/** 素材归属：media=媒体面板，audio=音频面板；缺省按 media 处理 */
\tpanel?: "media" | "audio";
}''',
)

p = SRC / "services" / "storage" / "service.ts"
patch(
    p,
    '''\t\t\tthumbnailUrl: mediaAsset.thumbnailUrl,
\t\t\tephemeral: mediaAsset.ephemeral,
\t\t};''',
    '''\t\t\tthumbnailUrl: mediaAsset.thumbnailUrl,
\t\t\tephemeral: mediaAsset.ephemeral,
\t\t\tpanel: mediaAsset.panel,
\t\t};''',
)
patch(
    p,
    '''\t\t\tthumbnailUrl: metadata.thumbnailUrl,
\t\t\tephemeral: metadata.ephemeral,
\t\t};''',
    '''\t\t\tthumbnailUrl: metadata.thumbnailUrl,
\t\t\tephemeral: metadata.ephemeral,
\t\t\tpanel: metadata.panel,
\t\t};''',
)

# 媒体面板：导入的素材固定归到 media，列表也只显示 media
p = SRC / "components" / "editor" / "panels" / "assets" / "views" / "media.tsx"
patch(
    p,
    '''\t\t\tfor (const asset of processedAssets) {
\t\t\t\tawait editor.media.addMediaAsset({
\t\t\t\t\tprojectId: activeProject.metadata.id,
\t\t\t\t\tasset,
\t\t\t\t});
\t\t\t}''',
    '''\t\t\tfor (const asset of processedAssets) {
\t\t\t\tawait editor.media.addMediaAsset({
\t\t\t\t\tprojectId: activeProject.metadata.id,
\t\t\t\t\t// 即使导入的是音频，也归媒体面板，不会跑到音频面板
\t\t\t\t\tasset: { ...asset, panel: "media" },
\t\t\t\t});
\t\t\t}''',
)
patch(
    p,
    '\t\tconst filtered = mediaFiles.filter((item) => !item.ephemeral);',
    '\t\tconst filtered = mediaFiles.filter(\n'
    '\t\t\t(item) => !item.ephemeral && (item.panel ?? "media") === "media",\n'
    '\t\t);',
)

 
# ------------------------------------------------ 右键菜单统一到面板级
# 有选中时，在面板任意空白处右键都能弹出「删除 / 取消选择」；
# 菜单里的「删除」删的是当前所有选中项，与右键点在哪一条素材上无关。
p = SRC / "components" / "editor" / "panels" / "assets" / "views" / "media.tsx"
patch(
    p,
    '''\t\t\t<div
\t\t\t\tclassName={`relative flex h-full flex-col gap-1 ${isDragOver ? "bg-accent/30" : ""}`}
\t\t\t\tref={panelRef}
\t\t\t\t{...dragProps}
\t\t\t>''',
    '''\t\t\t<ContextMenu>
\t\t\t\t<ContextMenuTrigger asChild>
\t\t\t\t\t<div
\t\t\t\t\t\tclassName={`relative flex h-full flex-col gap-1 ${isDragOver ? "bg-accent/30" : ""}`}
\t\t\t\t\t\tref={panelRef}
\t\t\t\t\t\t{...dragProps}
\t\t\t\t\t>''',
)
patch(
    p,
    '''\t\t\t</div>

\t\t\t<Dialog open={isUrlDialogOpen}''',
    '''\t\t\t\t\t</div>
\t\t\t\t</ContextMenuTrigger>
\t\t\t\t<ContextMenuContent>
\t\t\t\t\t<ContextMenuItem
\t\t\t\t\t\tvariant="destructive"
\t\t\t\t\t\tdisabled={marquee.selectedIds.length === 0}
\t\t\t\t\t\tonClick={() => void handleRemoveSelected()}
\t\t\t\t\t>
\t\t\t\t\t\t删除
\t\t\t\t\t</ContextMenuItem>
\t\t\t\t\t<ContextMenuSeparator />
\t\t\t\t\t<ContextMenuItem onClick={() => marquee.clearSelection()}>
\t\t\t\t\t\t取消选择
\t\t\t\t\t</ContextMenuItem>
\t\t\t\t</ContextMenuContent>
\t\t\t</ContextMenu>

\t\t\t<Dialog open={isUrlDialogOpen}''',
)
patch(
    p,
    '''\t\t\t<ContextMenuContent>
\t\t\t\t{isMultiSelected && (
\t\t\t\t\t<>
\t\t\t\t\t\t<ContextMenuItem
\t\t\t\t\t\t\tvariant="destructive"
\t\t\t\t\t\t\tonClick={() => void handleRemoveSelected()}
\t\t\t\t\t\t>
\t\t\t\t\t\t\t删除所选 {selectedIds.length} 项
\t\t\t\t\t\t</ContextMenuItem>
\t\t\t\t\t\t<ContextMenuSeparator />
\t\t\t\t\t</>
\t\t\t\t)}
\t\t\t\t<ContextMenuItem onClick={() => onExportClip({ item })}>''',
    '''\t\t\t<ContextMenuContent>
\t\t\t\t<ContextMenuItem onClick={() => onExportClip({ item })}>''',
)
patch(
    p,
    '''\t\t\t\t<ContextMenuItem
\t\t\t\t\tvariant="destructive"
\t\t\t\t\tonClick={(event) => onRemove({ event, id: item.id })}
\t\t\t\t>
\t\t\t\t\t{t("Delete")}
\t\t\t\t</ContextMenuItem>''',
    '''\t\t\t\t<ContextMenuItem
\t\t\t\t\tvariant="destructive"
\t\t\t\t\tonClick={(event) => {
\t\t\t\t\t\tevent.stopPropagation();
\t\t\t\t\t\t// 有选中就删掉全部选中项，与右键点在哪一条素材无关
\t\t\t\t\t\tif (selectedIds.length > 0) {
\t\t\t\t\t\t\tvoid handleRemoveSelected();
\t\t\t\t\t\t} else {
\t\t\t\t\t\t\tonRemove({ event, id: item.id });
\t\t\t\t\t\t}
\t\t\t\t\t}}
\t\t\t\t>
\t\t\t\t\t{t("Delete")}
\t\t\t\t</ContextMenuItem>''',
)


patch(
    p,
    "\nfunction GridView({",
    "\nexport function GridView({",
)
patch(
    p,
    "\nfunction MediaPreview({",
    "\nexport function MediaPreview({",
)
print("  exported GridView / MediaPreview（音频面板复用媒体卡片）")

# ------------------------------------------------ 右键 / 键盘删除只认「当前选中集合」
# 之前卡片各自带一套右键菜单，和面板菜单并存、互相打架，删除还可能拿到渲染时的旧快照。
# 现在只保留「面板级菜单」：右键任意位置（空白处、卡片上都一样）弹同一个菜单，
# 菜单里的删除/导出直接读 store 里的当前选中集合。
p = SRC / "components" / "editor" / "panels" / "assets" / "views" / "media.tsx"

text = p.read_text(encoding="utf-8")
_start = text.index("function MediaItemWithContextMenu({")
_end = text.index("\nexport function GridView({", _start)
text = (
    text[:_start]
    + '''function MediaItemWithContextMenu({
\titem,
\tchildren,
}: {
\titem: MediaAsset;
\tchildren: React.ReactNode;
}) {
	// 右键统一走面板级菜单；这里只保证「右键的那一条」在选中集合里：
	// 已经在集合里（框选了多条）就保持整个多选不动。
	return (
		<div
			onContextMenu={() => {
				const store = useAssetSelectionStore.getState();
				if (!store.selectedIds.includes(item.id)) {
					store.setSelectedIds({ ids: [item.id] });
				}
			}}
		>
			{children}
		</div>
	);
}
'''
    + text[_end:]
)
p.write_text(text, encoding="utf-8")
print("  patched media.tsx（卡片右键并入面板菜单，删除只认选中集合）")

patch(
    p,
    '''\tconst handleRemoveSelected = async () => {
\t\tif (!activeProject || marquee.selectedIds.length === 0) return;
\t\tconst ids = [...marquee.selectedIds];
\t\tmarquee.clearSelection();''',
    '''\t// 动作发生的那一刻读选中集合，避免用到渲染时的旧快照
\tconst selectedIdsNow = () => useAssetSelectionStore.getState().selectedIds;

\tconst handleExportSelected = () => {
\t\tconst ids = selectedIdsNow();
\t\tfor (const asset of filteredMediaItems) {
\t\t\tif (ids.includes(asset.id)) handleExportClip({ item: asset });
\t\t}
\t};

\tconst handleRemoveSelected = async () => {
\t\tconst current = selectedIdsNow();
\t\tif (!activeProject || current.length === 0) return;
\t\tconst ids = [...current];
\t\tmarquee.clearSelection();''',
)
patch(
    p,
    '''\t\t\t\t\t<ContextMenuItem onClick={() => marquee.clearSelection()}>
\t\t\t\t\t\t取消选择
\t\t\t\t\t</ContextMenuItem>''',
    '''\t\t\t\t\t<ContextMenuItem onClick={() => handleExportSelected()}>
\t\t\t\t\t\t导出所选
\t\t\t\t\t</ContextMenuItem>
\t\t\t\t\t<ContextMenuItem onClick={() => marquee.clearSelection()}>
\t\t\t\t\t\t取消选择
\t\t\t\t\t</ContextMenuItem>''',
)

# 键盘删除：捕获阶段 + 实时读取选中集合
for target in (p, SRC / "components" / "editor" / "panels" / "assets" / "views" / "sounds.tsx"):
    patch(
        target,
        '''\tuseEffect(() => {
\t\tif (marquee.selectedIds.length === 0) return;
\t\tconst onKeyDown = (event: KeyboardEvent) => {
\t\t\tif (event.key !== "Delete" && event.key !== "Backspace") return;
\t\t\tconst target = event.target as HTMLElement | null;
\t\t\tif (
\t\t\t\ttarget &&
\t\t\t\t(target.tagName === "INPUT" ||
\t\t\t\t\ttarget.tagName === "TEXTAREA" ||
\t\t\t\t\ttarget.isContentEditable)
\t\t\t) {
\t\t\t\treturn;
\t\t\t}
\t\t\tevent.preventDefault();
\t\t\tvoid handleRemoveSelected();
\t\t};
\t\twindow.addEventListener("keydown", onKeyDown);
\t\treturn () => window.removeEventListener("keydown", onKeyDown);
\t});''',
        '''\tuseEffect(() => {
\t\tconst onKeyDown = (event: KeyboardEvent) => {
\t\t\tif (event.key !== "Delete" && event.key !== "Backspace") return;
\t\t\tconst element = event.target as HTMLElement | null;
\t\t\tif (
\t\t\t\telement &&
\t\t\t\t(element.tagName === "INPUT" ||
\t\t\t\t\telement.tagName === "TEXTAREA" ||
\t\t\t\t\telement.isContentEditable)
\t\t\t) {
\t\t\t\treturn;
\t\t\t}
\t\t\tif (marquee.selectedIds.length === 0) return;
\t\t\t// 捕获阶段拦下，别让时间轴的 Delete 快捷键先吃掉
\t\t\tevent.preventDefault();
\t\t\tevent.stopPropagation();
\t\t\tvoid handleRemoveSelected();
\t\t};
\t\twindow.addEventListener("keydown", onKeyDown, true);
\t\treturn () => window.removeEventListener("keydown", onKeyDown, true);
\t});''',
    )

# ------------------------------------------------ 清掉卡片右键菜单删除后留下的死参数
# 卡片级右键菜单已经并进面板菜单：GridView / ListView / MediaView 里的
# onRemove、onExportClip 这一整条链都没人用了，按「成对出现」的特征一次清掉。
p = SRC / "components" / "editor" / "panels" / "assets" / "views" / "media.tsx"

patch_re(p, r"\n[ \t]*onRemove[^\n]*\n[ \t]*onExportClip[^\n]*", "", expect=8)
patch_re(
    p,
    re.escape('''\tconst handleRemove = async ({
\t\tevent,
\t\tid,
\t}: {
\t\tevent: React.MouseEvent;
\t\tid: string;
\t}) => {
\t\tevent.stopPropagation();

\t\tif (!activeProject) {
\t\t\ttoast.error(t("No active project"));
\t\t\treturn;
\t\t}

\t\tawait editor.media.removeMediaAsset({
\t\t\tprojectId: activeProject.metadata.id,
\t\t\tid,
\t\t});
\t};

'''),
    '',
)

# 音频面板：同样清理，并把「导出所选」补进面板菜单
p = SRC / "components" / "editor" / "panels" / "assets" / "views" / "sounds.tsx"
patch_re(p, r"\n[ \t]*onRemove[^\n]*\n[ \t]*onExportClip[^\n]*", "", expect=1)
patch_re(
    p,
    re.escape('''\tconst handleRemove = ({
\t\tevent,
\t\tid,
\t}: {
\t\tevent: React.MouseEvent;
\t\tid: string;
\t}) => {
\t\tevent.stopPropagation();
\t\tif (!project) return;
\t\tvoid editor.media.removeMediaAsset({ projectId: project.metadata.id, id });
\t};

'''),
    '',
)
patch(
    p,
    '''\tconst addElementAtTime = ({''',
    '''\tconst handleExportSelected = () => {
\t\tconst ids = marquee.selectedIds;
\t\tfor (const asset of audioAssets) {
\t\t\tif (ids.includes(asset.id)) handleExportClip({ item: asset });
\t\t}
\t};

\tconst addElementAtTime = ({''',
)
patch(
    p,
    '''\t\t\t\t\t<ContextMenuItem onClick={() => marquee.clearSelection()}>
\t\t\t\t\t\t取消选择
\t\t\t\t\t</ContextMenuItem>''',
    '''\t\t\t\t\t<ContextMenuItem onClick={() => handleExportSelected()}>
\t\t\t\t\t\t导出所选
\t\t\t\t\t</ContextMenuItem>
\t\t\t\t\t<ContextMenuItem onClick={() => marquee.clearSelection()}>
\t\t\t\t\t\t取消选择
\t\t\t\t\t</ContextMenuItem>''',
)
# ------------------------------------------------ 左键选中也能用 Delete 删
# 点卡片走的是「预览选中」，没写进素材选中集合，键盘删除就看不到东西。
# 这里让左键选中同时进选中集合（单选），取消选中时一起清掉。
p = SRC / "components" / "editor" / "panels" / "assets" / "views" / "media.tsx"
patch(
    p,
    '''\tconst handleSelectMedia = ({ asset }: { asset: MediaAsset }) => {
\t\tconst store = useMediaPreviewStore.getState();
\t\tif (store.selectedMediaId === asset.id) {
\t\t\tstore.clearSelection();
\t\t} else {
\t\t\tstore.selectMedia({ mediaId: asset.id });
\t\t}
\t};''',
    '''\tconst handleSelectMedia = ({ asset }: { asset: MediaAsset }) => {
\t\tconst preview = useMediaPreviewStore.getState();
\t\tconst selection = useAssetSelectionStore.getState();
\t\tif (preview.selectedMediaId === asset.id) {
\t\t\tpreview.clearSelection();
\t\t\tselection.clear();
\t\t} else {
\t\t\tpreview.selectMedia({ mediaId: asset.id });
\t\t\t// 左键选中同时进「素材选中集合」，这样 Delete 键能直接删
\t\t\tselection.setSelectedIds({ ids: [asset.id] });
\t\t}
\t};''',
)
patch(
    p,
    '''\tconst handleClearSelection = () => {
\t\tuseMediaPreviewStore.getState().clearSelection();
\t};''',
    '''\tconst handleClearSelection = () => {
\t\tuseMediaPreviewStore.getState().clearSelection();
\t\tuseAssetSelectionStore.getState().clear();
\t};''',
)

p = SRC / "components" / "editor" / "panels" / "assets" / "views" / "sounds.tsx"
patch(
    p,
    'import { useMediaPreviewStore } from "@/stores/media-preview-store";',
    'import { useAssetSelectionStore } from "@/stores/asset-selection-store";\n'
    'import { useMediaPreviewStore } from "@/stores/media-preview-store";',
)
patch(
    p,
    '''\tconst handleSelect = ({ asset }: { asset: MediaAsset }) => {
\t\tconst store = useMediaPreviewStore.getState();
\t\tif (store.selectedMediaId === asset.id) {
\t\t\tstore.clearSelection();
\t\t} else {
\t\t\tstore.selectMedia({ mediaId: asset.id });
\t\t}
\t};''',
    '''\tconst handleSelect = ({ asset }: { asset: MediaAsset }) => {
\t\tconst preview = useMediaPreviewStore.getState();
\t\tconst selection = useAssetSelectionStore.getState();
\t\tif (preview.selectedMediaId === asset.id) {
\t\t\tpreview.clearSelection();
\t\t\tselection.clear();
\t\t} else {
\t\t\tpreview.selectMedia({ mediaId: asset.id });
\t\t\t// 左键选中同时进「素材选中集合」，这样 Delete 键能直接删
\t\t\tselection.setSelectedIds({ ids: [asset.id] });
\t\t}
\t};''',
)
patch(
    p,
    '''\t\tconst ids = [...marquee.selectedIds];
\t\tmarquee.clearSelection();
\t\tfor (const id of ids) {
\t\t\tawait editor.media.removeMediaAsset({ projectId: project.metadata.id, id });
\t\t}''',
    '''\t\tconst ids = [...marquee.selectedIds];
\t\tmarquee.clearSelection();
\t\t// 删掉的素材不能继续留在播放框里
\t\tuseMediaPreviewStore.getState().clearSelection();
\t\tfor (const id of ids) {
\t\t\tawait editor.media.removeMediaAsset({ projectId: project.metadata.id, id });
\t\t}''',
)

# --------------------------------------------------- 变速：预设按钮 -> 滑条
# 上游把「Playback Speed」做成一排 0.25x/0.5x/... 按钮，再加一个独立的
# 「Custom」数字输入框。YuanCut 里改成和缩放/旋转/透明度一样的排版：
# 一行「标签 + 滑条 + 数字输入框」（0.25~4.0，step 0.05）。
SPEED_GROUP_RE = (
    r'^([ \t]*)<PropertyItem direction="column">\s*'
    r'<PropertyItemLabel>\{t\("Playback Speed"\)\}</PropertyItemLabel>\s*'
    r'<PropertyItemValue>\s*'
    r'<div className="flex flex-wrap gap-1\.5">.*?</div>\s*'
    r'</PropertyItemValue>\s*'
    r'</PropertyItem>\s*'
    r'<PropertyItem>\s*'
    r'<PropertyItemLabel>\{t\("Custom"\)\}</PropertyItemLabel>\s*'
    r'<PropertyItemValue>\s*'
    r'(<div className="flex items-center gap-1">\s*<Input\b.*?/>.*?</div>)\s*'
    r'</PropertyItemValue>\s*'
    r'</PropertyItem>'
)


def _indent_lines(block: str, indent: str) -> list[str]:
    """按相对缩进重排 JSX 片段：首行作为基准，其余行保留原有的层级。"""
    lines = [line for line in block.splitlines() if line.strip()]
    if not lines:
        return []
    head, rest = lines[0], lines[1:]
    if rest:
        common = min(len(line) - len(line.lstrip()) for line in rest)
        rest = [
            line[common:] if len(line) - len(line.lstrip()) >= common else line.strip()
            for line in rest
        ]
    return [indent + head.strip()] + [indent + line for line in rest]


def build_speed_row(match: re.Match[str]) -> str:
    """把两个 PropertyItem 合成与缩放/旋转一致的单行滑条。"""
    indent = match.group(1)
    input_match = re.search(r"<Input\b.*?/>", match.group(2), re.S)
    if input_match is None:
        raise SystemExit("speed number input not found")
    # 和缩放/旋转的数字框同宽（w-14），不要独占一行的 w-full
    number_input = input_match.group(0).replace("h-7 w-full", "h-7 w-14")

    row = [
        '<PropertyItem direction="column">',
        '\t<PropertyItemLabel>{t("Playback Speed")}</PropertyItemLabel>',
        "\t<PropertyItemValue>",
        '\t\t<div className="flex items-center gap-2">',
        "\t\t\t<Slider",
        "\t\t\t\tvalue={[currentSpeed]}",
        "\t\t\t\tmin={0.25}",
        "\t\t\t\tmax={4}",
        "\t\t\t\tstep={0.05}",
        '\t\t\t\tclassName="w-full"',
        "\t\t\t\tonValueChange={([value]) => {",
        "\t\t\t\t\tapplySpeedChange({",
        "\t\t\t\t\t\tnewRate: Math.round(value * 100) / 100,",
        "\t\t\t\t\t\tpushHistory: false,",
        "\t\t\t\t\t});",
        "\t\t\t\t}}",
        "\t\t\t\tonValueCommit={([value]) => {",
        "\t\t\t\t\tapplySpeedChange({",
        "\t\t\t\t\t\tnewRate: Math.round(value * 100) / 100,",
        "\t\t\t\t\t\tpushHistory: true,",
        "\t\t\t\t\t});",
        "\t\t\t\t}}",
        "\t\t\t/>",
    ]
    row.extend(_indent_lines(number_input, "\t\t\t"))
    row.extend(
        [
            "\t\t</div>",
            "\t</PropertyItemValue>",
            "</PropertyItem>",
        ]
    )
    return "\n".join(indent + line if line else indent for line in row)


def patch_speed_slider(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if "value={[currentSpeed]}" in text:
        print(f"  keep    {path.relative_to(ROOT)} (speed slider row)")
        return

    text2, count = re.subn(SPEED_GROUP_RE, build_speed_row, text, flags=re.S | re.M)
    if count == 0:
        raise SystemExit(f"speed preset block not found in {path}")
    if count != 1:
        raise SystemExit(f"{path}: expected 1 speed block, got {count}")
    path.write_text(text2, encoding="utf-8")
    print(f"  patched {path.relative_to(ROOT)} (speed slider row)")


for _panel in ("audio-properties.tsx", "video-properties.tsx"):
    _panel_path = SRC / "components" / "editor" / "panels" / "properties" / _panel
    patch_speed_slider(_panel_path)
    # SPEED_PRESETS 不再使用，去掉导入避免 lint 噪声
    patch(
        _panel_path,
        'import { SPEED_PRESETS, formatSpeedLabel } from "@/lib/timeline/speed-utils";',
        'import { formatSpeedLabel } from "@/lib/timeline/speed-utils";',
        required=False,
    )

print("Done. Next: pnpm install && pnpm exec next build (see tools/build_ui.sh)")


# ------------------------------------------------------------ 人声美化面板
TIMELINE_TYPES = SRC / "types" / "timeline.ts"
_tt = TIMELINE_TYPES.read_text(encoding="utf-8")
if "voiceBeautifyMediaId" not in _tt:
    _tt = _tt.replace(
        'export interface UploadAudioElement extends BaseAudioElement {\n\tsourceType: "upload";\n\tmediaId: string;\n}',
        'export interface UploadAudioElement extends BaseAudioElement {\n'
        '\tsourceType: "upload";\n'
        '\tmediaId: string;\n'
        '\t/** 人声美化：勾选后用处理结果当音源，取消勾选切回原素材 */\n'
        '\tvoiceBeautifyEnabled?: boolean;\n'
        '\t/** 人声美化：处理结果（wav）素材 id */\n'
        '\tvoiceBeautifyMediaId?: string;\n'
        '\t/** 人声美化：被处理的原始素材 id（留着以便重新处理 / 还原） */\n'
        '\tvoiceBeautifySourceId?: string;\n'
        '\t/** 人声美化：原始素材名称（取消勾选时还原） */\n'
        '\tvoiceBeautifySourceName?: string;\n'
        '\t/** 人声美化：上次使用的强度 0~1 */\n'
        '\tvoiceBeautifyStrength?: number;\n'
        '}',
        1,
    )
    _tt = _tt.replace(
        '\tplaybackRate?: number;\n\treversed?: boolean;\n}\n\nexport interface ImageElement',
        '\tplaybackRate?: number;\n'
        '\treversed?: boolean;\n'
        '\t/** 人声美化：勾选后用 voiceBeautifyMediaId 的声音替换画面自带音轨 */\n'
        '\tvoiceBeautifyEnabled?: boolean;\n'
        '\t/** 人声美化：处理结果（wav）素材 id */\n'
        '\tvoiceBeautifyMediaId?: string;\n'
        '\t/** 人声美化：上次使用的强度 0~1 */\n'
        '\tvoiceBeautifyStrength?: number;\n'
        '}\n\nexport interface ImageElement',
        1,
    )
    TIMELINE_TYPES.write_text(_tt, encoding="utf-8")
    print(f"  patched {TIMELINE_TYPES.relative_to(ROOT)} (人声美化字段)")


VOCAL_LIB = SRC / "lib" / "audio" / "vocal-beautify.ts"
VOCAL_LIB_SOURCE = '''import { withBasePath } from "@/lib/base-path";
import { uploadAssetBlob } from "@/lib/export";
import type { EditorCore } from "@/core";
import type { MediaAsset } from "@/types/assets";

export type VocalBeautifyProgress = {
\tstatus: string;
\tstage: string;
\tprogress: number;
\terror: string;
};

const STAGE_LABELS: Record<string, string> = {
\tqueued: "排队中",
\tstarting: "启动引擎",
\tdecoding: "解码音频",
\tloading: "载入模型",
\tenhancing: "神经网络增强",
\tdsp: "音色塑形",
\tmixing: "合成输出",
\tdone: "完成",
};

export function describeVocalBeautify(state: VocalBeautifyProgress): string {
\tif (state.status === "done") return "处理完成";
\tconst label = STAGE_LABELS[state.stage] ?? state.stage ?? "";
\tconst percent = Math.round((state.progress ?? 0) * 100);
\treturn percent > 0 ? `${label} ${percent}%` : label;
}

async function readDuration(buffer: ArrayBuffer): Promise<number | null> {
\ttry {
\t\tconst context = new AudioContext();
\t\tconst decoded = await context.decodeAudioData(buffer.slice(0));
\t\tconst duration = decoded.duration;
\t\tvoid context.close();
\t\treturn Number.isFinite(duration) ? duration : null;
\t} catch {
\t\treturn null;
\t}
}

function extensionOf(name: string): string {
\tconst match = /\\.([A-Za-z0-9]{2,5})$/.exec(name);
\treturn (match?.[1] ?? "wav").toLowerCase();
}

/**
 * 人声美化：原素材 → 服务端（MossFormer2_SE_48K + DSP 增强链）→ 新音频素材。
 *
 * 结果作为独立音频素材返回：音频片段把 mediaId 切过去，视频片段把结果挂到
 * voiceBeautifyMediaId（画面不动、只换声音）。预览和导出用的都是这个 wav，
 * 所以预览与导出天然一致。
 */
export async function applyVocalBeautify({
\teditor,
\tsource,
\tstrength,
\tonProgress,
}: {
\teditor: EditorCore;
\t/** 原始素材：音频片段，或带音轨的视频片段 */
\tsource: MediaAsset;
\tstrength: number;
\tonProgress?: (state: VocalBeautifyProgress) => void;
}): Promise<{ mediaId: string; name: string }> {

\tconst uploaded = await uploadAssetBlob({
\t\tassetId: source.id,
\t\tblob: source.file,
\t\text: extensionOf(source.name),
\t});
\tif (!uploaded) throw new Error("素材上传到服务端失败");

\tconst started = await fetch(withBasePath("/api/voice/beautify"), {
\t\tmethod: "POST",
\t\theaders: { "Content-Type": "application/json" },
\t\tbody: JSON.stringify({ assetId: source.id, strength }),
\t});
\tif (!started.ok) {
\t\tconst detail = (await started.json().catch(() => ({}))) as { error?: string };
\t\tthrow new Error(detail.error || `服务端拒绝（${started.status}）`);
\t}
\tconst { jobId } = (await started.json()) as { jobId?: string };
\tif (!jobId) throw new Error("服务端没有返回任务号");

\tlet state: VocalBeautifyProgress = {
\t\tstatus: "queued",
\t\tstage: "queued",
\t\tprogress: 0,
\t\terror: "",
\t};
\tfor (let attempt = 0; attempt < 3600; attempt += 1) {
\t\tawait new Promise((resolve) => setTimeout(resolve, 500));
\t\tconst response = await fetch(
\t\t\twithBasePath(`/api/voice/progress?jobId=${encodeURIComponent(jobId)}`),
\t\t);
\t\tif (!response.ok) throw new Error(`进度查询失败（${response.status}）`);
\t\tstate = (await response.json()) as VocalBeautifyProgress;
\t\tonProgress?.(state);
\t\tif (state.status === "done" || state.status === "error") break;
\t}
\tif (state.status === "error") throw new Error(state.error || "处理失败");
\tif (state.status !== "done") throw new Error("处理超时");

\tconst result = await fetch(withBasePath(`/api/voice/result?jobId=${encodeURIComponent(jobId)}`));
\tif (!result.ok) throw new Error(`取回结果失败（${result.status}）`);
\tconst buffer = await result.arrayBuffer();

\tconst baseName = source.name.replace(/\\.[^.]+$/, "");
\tconst name = `${baseName}-人声美化.wav`;
\tconst file = new File([buffer], name, { type: "audio/wav" });
\tconst url = URL.createObjectURL(file);
\tconst duration = await readDuration(buffer);
\tconst projectId = editor.project.getActive().metadata.id;
\tconst mediaId = await editor.media.addMediaAsset({
\t\tprojectId,
\t\tasset: {
\t\t\tname,
\t\t\ttype: "audio",
\t\t\tfile,
\t\t\turl,
\t\t\tduration: duration ?? source.duration,
\t\t\tephemeral: true,
\t\t},
\t});

\treturn { mediaId, name };
}
'''
VOCAL_LIB.parent.mkdir(parents=True, exist_ok=True)
VOCAL_LIB.write_text(VOCAL_LIB_SOURCE, encoding="utf-8")
print(f"  wrote {VOCAL_LIB.relative_to(ROOT)} (人声美化客户端)")


VOICE_PANEL_DIR = SRC / "components" / "editor" / "panels" / "properties"

# 人声美化那一行：音频 / 视频属性面板共用，样式对齐音量、速度；
# 前置勾选框，不勾就是灰的不可编辑，勾上才处理。
VOCAL_ROW = VOICE_PANEL_DIR / "vocal-beautify-row.tsx"
VOCAL_ROW_SOURCE = '''"use client";

import { Slider } from "@/components/ui/slider";
import { cn } from "@/utils/ui";
import { PropertyItem, PropertyItemLabel, PropertyItemValue } from "./property-item";

export function VocalBeautifyRow({
\tenabled,
\tbusy,
\tstrength,
\tstatus,
\tprogress,
\tneedsApply,
\tonToggle,
\tonStrengthChange,
\tonApply,
}: {
\t/** 勾选状态：点了立刻变，不等处理完 */
\tenabled: boolean;
\tbusy: boolean;
\tstrength: number;
\tstatus: string;
\t/** 0~1 的处理进度，null 表示没在跑 */
\tprogress: number | null;
\tneedsApply: boolean;
\tonToggle: (checked: boolean) => void;
\tonStrengthChange: (value: number) => void;
\tonApply: () => void;
}) {
\tconst percent = Math.round(strength * 100);
\tconst done = Math.max(0, Math.min(1, progress ?? 0));
\treturn (
\t\t<div className="flex flex-col border-t last:border-b">
\t\t\t{/* 勾选框就在标题前面：不勾 = 灰的不可编辑，勾上 = 处理 */}
\t\t\t<div className="flex items-center justify-between p-4">
\t\t\t\t<label className="flex cursor-pointer items-center gap-1.5 select-none">
\t\t\t\t\t<input
\t\t\t\t\t\ttype="checkbox"
\t\t\t\t\t\tclassName="accent-primary size-3.5 cursor-pointer"
\t\t\t\t\t\tchecked={enabled}
\t\t\t\t\t\tonChange={(event) => {
\t\t\t\t\t\t\tonToggle(event.target.checked);
\t\t\t\t\t\t}}
\t\t\t\t\t/>
\t\t\t\t\t<span
\t\t\t\t\t\tclassName={cn(
\t\t\t\t\t\t\t"text-xs font-medium",
\t\t\t\t\t\t\tenabled ? "text-foreground" : "text-muted-foreground",
\t\t\t\t\t\t)}
\t\t\t\t\t>
\t\t\t\t\t\t人声美化
\t\t\t\t\t</span>
\t\t\t\t</label>
\t\t\t</div>
\t\t\t<div className={cn("p-3 pt-0", enabled ? "" : "opacity-40")}>
\t\t\t\t<PropertyItem>
\t\t\t\t\t<PropertyItemLabel>强度</PropertyItemLabel>
\t\t\t\t\t<PropertyItemValue>
\t\t\t\t\t\t<div className="flex items-center gap-2">
\t\t\t\t\t\t\t<Slider
\t\t\t\t\t\t\t\tvalue={[percent]}
\t\t\t\t\t\t\t\tmin={0}
\t\t\t\t\t\t\t\tmax={100}
\t\t\t\t\t\t\t\tstep={1}
\t\t\t\t\t\t\t\tdisabled={!enabled || busy}
\t\t\t\t\t\t\t\tonValueChange={([value]) => {
\t\t\t\t\t\t\t\t\tonStrengthChange((value ?? 80) / 100);
\t\t\t\t\t\t\t\t}}
\t\t\t\t\t\t\t\tclassName="flex-1"
\t\t\t\t\t\t\t/>
\t\t\t\t\t\t\t<span className="text-muted-foreground w-9 text-right text-xs tabular-nums">
\t\t\t\t\t\t\t\t{percent}%
\t\t\t\t\t\t\t</span>
\t\t\t\t\t\t</div>
\t\t\t\t\t</PropertyItemValue>
\t\t\t\t</PropertyItem>
\t\t\t\t<div className="mt-2 space-y-1">
\t\t\t\t\t{busy ? (
\t\t\t\t\t\t<>
\t\t\t\t\t\t\t<div className="bg-accent h-1 w-full overflow-hidden rounded-full">
\t\t\t\t\t\t\t\t<div
\t\t\t\t\t\t\t\t\tclassName="bg-primary h-full rounded-full transition-[width] duration-300"
\t\t\t\t\t\t\t\t\tstyle={{ width: `${Math.round(done * 100)}%` }}
\t\t\t\t\t\t\t\t/>
\t\t\t\t\t\t\t</div>
\t\t\t\t\t\t\t<span className="text-foreground/80 block text-[11px] leading-tight">
\t\t\t\t\t\t\t\t{status || "处理中…"}
\t\t\t\t\t\t\t</span>
\t\t\t\t\t\t</>
\t\t\t\t\t) : (
\t\t\t\t\t\t<span className="text-muted-foreground block text-[11px] leading-tight">
\t\t\t\t\t\t\t{enabled
\t\t\t\t\t\t\t\t? status || "去混响 / 去喷麦 / 降噪 / 提升清晰度"
\t\t\t\t\t\t\t\t: "不勾选保持原声"}
\t\t\t\t\t\t</span>
\t\t\t\t\t)}
\t\t\t\t\t{enabled && needsApply && !busy ? (
\t\t\t\t\t\t<button
\t\t\t\t\t\t\ttype="button"
\t\t\t\t\t\t\tonClick={onApply}
\t\t\t\t\t\t\tclassName="bg-accent hover:bg-accent/80 h-7 w-full rounded-sm px-2 text-xs"
\t\t\t\t\t\t>
\t\t\t\t\t\t\t按新强度重新处理
\t\t\t\t\t\t</button>
\t\t\t\t\t) : null}
\t\t\t\t</div>
\t\t\t</div>
\t\t</div>
\t);
}
'''
VOCAL_ROW.parent.mkdir(parents=True, exist_ok=True)
VOCAL_ROW.write_text(VOCAL_ROW_SOURCE, encoding="utf-8")
print(f"  wrote {VOCAL_ROW.relative_to(ROOT)} (人声美化勾选行)")


VOCAL_PANEL = VOICE_PANEL_DIR / "audio-properties.tsx"
_panel = VOCAL_PANEL.read_text(encoding="utf-8")

VOCAL_HANDLER = '''\tconst beautifySourceId =
\t\t(element as UploadAudioElement).voiceBeautifySourceId ??
\t\t(element as UploadAudioElement).mediaId;
\tconst beautifyStoredEnabled =
\t\t(element as UploadAudioElement).voiceBeautifyEnabled ??
\t\tBoolean((element as UploadAudioElement).voiceBeautifySourceId);
\tconst beautifyAppliedStrength = (element as UploadAudioElement)
\t\t.voiceBeautifyStrength;
\t// 点击后先用本地状态把勾打上（不等处理完），等片段字段生效再交回去
\tconst [beautifyOverride, setBeautifyOverride] = useState<boolean | null>(null);
\tconst beautifyEnabled = beautifyOverride ?? beautifyStoredEnabled;
\tconst [beautifyProgress, setBeautifyProgress] = useState<number | null>(null);

\tuseEffect(() => {
\t\tif (beautifyOverride !== null && beautifyOverride === beautifyStoredEnabled) {
\t\t\tsetBeautifyOverride(null);
\t\t}
\t}, [beautifyOverride, beautifyStoredEnabled]);

\tconst runVocalBeautify = async () => {
\t\tif (element.sourceType !== "upload" || beautifyBusy) return;
\t\tconst upload = element as UploadAudioElement;
\t\tconst source = editor.media
\t\t\t.getAssets()
\t\t\t.find((asset) => asset.id === beautifySourceId);
\t\tif (!source) {
\t\t\tsetBeautifyOverride(null);
\t\t\tsetBeautifyStatus("找不到原始素材");
\t\t\treturn;
\t\t}
\t\tsetBeautifyBusy(true);
\t\tsetBeautifyProgress(0);
\t\tsetBeautifyStatus("准备中…");
\t\ttry {
\t\t\tconst result = await applyVocalBeautify({
\t\t\t\teditor,
\t\t\t\tsource,
\t\t\t\tstrength: beautifyStrength,
\t\t\t\tonProgress: (state) => {
\t\t\t\t\tsetBeautifyStatus(describeVocalBeautify(state));
\t\t\t\t\tsetBeautifyProgress(state.progress ?? 0);
\t\t\t\t},
\t\t\t});
\t\t\tupdateElement({
\t\t\t\tupdates: {
\t\t\t\t\tmediaId: result.mediaId,
\t\t\t\t\tname: result.name,
\t\t\t\t\tvoiceBeautifyEnabled: true,
\t\t\t\t\tvoiceBeautifyMediaId: result.mediaId,
\t\t\t\t\tvoiceBeautifySourceId: beautifySourceId,
\t\t\t\t\tvoiceBeautifySourceName: upload.voiceBeautifySourceName ?? upload.name,
\t\t\t\t\tvoiceBeautifyStrength: beautifyStrength,
\t\t\t\t},
\t\t\t});
\t\t\tsetBeautifyStatus("已处理：预览与导出都用处理后的音频");
\t\t} catch (error) {
\t\t\tsetBeautifyOverride(null);
\t\t\tsetBeautifyStatus(`失败：${(error as Error).message}`);
\t\t} finally {
\t\t\tsetBeautifyBusy(false);
\t\t\tsetBeautifyProgress(null);
\t\t}
\t};

\tconst toggleVocalBeautify = (checked: boolean) => {
\t\tif (element.sourceType !== "upload" || beautifyBusy) return;
\t\tconst upload = element as UploadAudioElement;
\t\tsetBeautifyOverride(checked);
\t\tif (!checked) {
\t\t\tupdateElement({
\t\t\t\tupdates: {
\t\t\t\t\tmediaId: beautifySourceId,
\t\t\t\t\tname:
\t\t\t\t\t\tupload.voiceBeautifySourceName ??
\t\t\t\t\t\tupload.name.replace(/-人声美化(?=\\.|$)/, ""),
\t\t\t\t\tvoiceBeautifyEnabled: false,
\t\t\t\t},
\t\t\t});
\t\t\tsetBeautifyStatus("");
\t\t\treturn;
\t\t}
\t\tif (upload.voiceBeautifyMediaId) {
\t\t\tconst processed = editor.media
\t\t\t\t.getAssets()
\t\t\t\t.find((asset) => asset.id === upload.voiceBeautifyMediaId);
\t\t\tupdateElement({
\t\t\t\tupdates: {
\t\t\t\t\tmediaId: upload.voiceBeautifyMediaId,
\t\t\t\t\tname: processed?.name ?? upload.name,
\t\t\t\t\tvoiceBeautifyEnabled: true,
\t\t\t\t},
\t\t\t});
\t\t\tsetBeautifyStatus("");
\t\t\treturn;
\t\t}
\t\tvoid runVocalBeautify();
\t};

'''

VOCAL_CARD = '''\t\t\t\t{element.sourceType === "upload" ? (
\t\t\t\t\t<VocalBeautifyRow
\t\t\t\t\t\tenabled={beautifyEnabled}
\t\t\t\t\t\tbusy={beautifyBusy}
\t\t\t\t\t\tstrength={beautifyStrength}
\t\t\t\t\t\tstatus={beautifyStatus}
\t\t\t\t\t\tprogress={beautifyProgress}
\t\t\t\t\t\tneedsApply={
\t\t\t\t\t\t\tbeautifyEnabled &&
\t\t\t\t\t\t\tMath.abs((beautifyAppliedStrength ?? -1) - beautifyStrength) > 0.001
\t\t\t\t\t\t}
\t\t\t\t\t\tonToggle={toggleVocalBeautify}
\t\t\t\t\t\tonStrengthChange={(value) => {
\t\t\t\t\t\t\tsetBeautifyStrength(clamp({ value, min: 0, max: 1 }));
\t\t\t\t\t\t}}
\t\t\t\t\t\tonApply={() => {
\t\t\t\t\t\t\tvoid runVocalBeautify();
\t\t\t\t\t\t}}
\t\t\t\t\t/>
\t\t\t\t) : (
\t\t\t\t\t<PropertyGroup title="人声美化" collapsible={false}>
\t\t\t\t\t\t<span className="text-muted-foreground text-[11px]">
\t\t\t\t\t\t\t仅支持本地音频素材
\t\t\t\t\t\t</span>
\t\t\t\t\t</PropertyGroup>
\t\t\t\t)}

'''

if "applyVocalBeautify" not in _panel:
    _panel = _panel.replace(
        'import type { AudioElement } from "@/types/timeline";',
        'import type { AudioElement, UploadAudioElement } from "@/types/timeline";\n'
        'import {\n\tapplyVocalBeautify,\n\tdescribeVocalBeautify,\n} from "@/lib/audio/vocal-beautify";\n'
        'import { VocalBeautifyRow } from "./vocal-beautify-row";',
        1,
    )
    _panel = _panel.replace(
        'import { useReducer, useRef } from "react";',
        'import { useEffect, useReducer, useRef, useState } from "react";',
        1,
    )
    _panel = _panel.replace(
        "\tconst [, forceRender] = useReducer((x: number) => x + 1, 0);",
        "\tconst [, forceRender] = useReducer((x: number) => x + 1, 0);\n"
        "\tconst [beautifyBusy, setBeautifyBusy] = useState(false);\n"
        "\tconst [beautifyStatus, setBeautifyStatus] = useState(\"\");\n"
        "\tconst [beautifyStrength, setBeautifyStrength] = useState(\n"
        "\t\t(element as UploadAudioElement).voiceBeautifyStrength ?? 0.8,\n"
        "\t);\n",
        1,
    )
    _panel = _panel.replace(
        '\treturn (\n\t\t<div className="flex h-full flex-col">',
        VOCAL_HANDLER + '\treturn (\n\t\t<div className="flex h-full flex-col">',
        1,
    )
    _marker = "\t\t\t</PanelBaseView>"
    _index = _panel.rfind(_marker)
    if _index == -1:
        raise SystemExit(f"panel view marker not found in {VOCAL_PANEL}")
    _panel = _panel[:_index] + VOCAL_CARD + _panel[_index:]
    VOCAL_PANEL.write_text(_panel, encoding="utf-8")
    print(f"  patched {VOCAL_PANEL.relative_to(ROOT)} (人声美化卡片)")


# ---------------------------------------------------- 只保留中英文 / 去掉反馈
# 上游把语言做成 URL 段（/[locale]/...），这里把 locale 收窄到 en/zh，
# 并删掉其它语言的翻译包，静态导出就不会再生成那 10 套页面。
_I18N_CONFIG = WEB / "src" / "i18n.config.ts"
if _I18N_CONFIG.is_file():
    _t = _I18N_CONFIG.read_text(encoding="utf-8")
    _start = _t.find("locales: [")
    if _start != -1:
        _end = _t.index("]", _start) + 1
        _t = _t[:_start] + 'locales: ["en", "zh"]' + _t[_end:]
        _I18N_CONFIG.write_text(_t, encoding="utf-8")
        print("  patched src/i18n.config.ts (locales -> en/zh)")

_LANG_TOGGLE = WEB / "src" / "components" / "language-toggle.tsx"
if _LANG_TOGGLE.is_file():
    _t = _LANG_TOGGLE.read_text(encoding="utf-8")
    _start = _t.find("const SUPPORTED_LANGUAGES = [")
    if _start != -1:
        _end = _t.index("] as const;", _start) + len("] as const;")
        _replacement = (
            "const SUPPORTED_LANGUAGES = [\n"
            '\t{ code: "en", label: "English" },\n'
            '\t{ code: "zh", label: "中文" },\n'
            "] as const;"
        )
        _t = _t[:_start] + _replacement + _t[_end:]
        _LANG_TOGGLE.write_text(_t, encoding="utf-8")
        print("  patched src/components/language-toggle.tsx (语言列表只留中英)")

_LOCALES_DIR = WEB / "public" / "locales"
if _LOCALES_DIR.is_dir():
    for _entry in sorted(_LOCALES_DIR.iterdir()):
        if _entry.is_dir() and _entry.name not in ("en", "zh"):
            shutil.rmtree(_entry)
            print(f"  removed public/locales/{_entry.name}/")

# 反馈：编辑器头部按钮、移动端菜单项，以及弹窗/服务本身
for _rel in ("src/components/feedback", "src/services/feedback"):
    _target = WEB / _rel
    if _target.is_dir():
        shutil.rmtree(_target)
        print(f"  removed {_rel}/")

_EDITOR_HEADER = WEB / "src" / "components" / "editor" / "editor-header.tsx"
if _EDITOR_HEADER.is_file():
    _t = _EDITOR_HEADER.read_text(encoding="utf-8")
    if "FeedbackTrigger" in _t:
        _t = _t.replace(
            'import { FeedbackTrigger } from "@/components/feedback/feedback-trigger";\n', ""
        )
        _t = re.sub(r"[ \t]*<FeedbackTrigger>.*?</FeedbackTrigger>\n", "", _t, flags=re.S)
        # 这个图标只服务于反馈按钮，一并删掉
        _t = _t.replace("\tBubbleChatIcon,\n", "")
        _EDITOR_HEADER.write_text(_t, encoding="utf-8")
        print("  patched editor-header.tsx (去掉反馈按钮)")

_MOBILE_HEADER = WEB / "src" / "components" / "editor" / "mobile" / "mobile-header.tsx"
if _MOBILE_HEADER.is_file():
    _t = _MOBILE_HEADER.read_text(encoding="utf-8")
    if "FeedbackDialog" in _t:
        _t = _t.replace(
            'import { FeedbackDialog } from "@/components/feedback/feedback-dialog";\n', ""
        )
        _t = _t.replace("\tconst [feedbackOpen, setFeedbackOpen] = useState(false);\n", "")
        _t = re.sub(r"\n\tconst handleFeedback = \(\) => \{.*?\n\t\};\n", "\n", _t, flags=re.S)
        _t = re.sub(
            r"\n[ \t]*<DropdownMenuItem[^>]*onClick=\{handleFeedback\}.*?</DropdownMenuItem>",
            "",
            _t,
            flags=re.S,
        )
        _t = _t.replace(
            "\n\t\t\t<FeedbackDialog open={feedbackOpen} onOpenChange={setFeedbackOpen} />", ""
        )
        _t = _t.replace("\tBubbleChatIcon,\n", "")
        _MOBILE_HEADER.write_text(_t, encoding="utf-8")
        print("  patched mobile-header.tsx (去掉反馈菜单项)")


# ------------------------------------------------- 字幕导入（SRT / TXT，与导出对应）
SUBTITLE_FORMAT = SRC / "lib" / "transcription" / "subtitle-format.ts"
SUBTITLE_FORMAT_SOURCE = '''/**
 * 字幕文件解析：与导出的两种格式一一对应。
 *
 * - SRT：带时间码（`HH:MM:SS,mmm --> HH:MM:SS,mmm`），导入后按文件时间落到时间线
 * - TXT：一行一条字幕，导入后从播放头开始按模板时长顺排
 *
 * 导出侧见 `@/lib/export` 的 buildSubtitleFile：srt 用 `,` 分隔毫秒，txt 一行一条。
 */

export type SubtitleFormat = "srt" | "txt";

export interface SubtitleCue {
\t/** 相对字幕文件开头的开始时间（秒） */
\tstart: number;
\t/** 结束时间（秒），至少比 start 晚 0.2 秒 */
\tend: number;
\ttext: string;
}

const TIMECODE =
\t/(\\d{1,3}):(\\d{2}):(\\d{2})[,.](\\d{1,4})\\s*-->\\s*(\\d{1,3}):(\\d{2}):(\\d{2})[,.](\\d{1,4})/;

function normalize({ text }: { text: string }): string {
\treturn text.replace(/^\\uFEFF/, "").replace(/\\r\\n?/g, "\\n");
}

function toSeconds({
\thours,
\tminutes,
\tseconds,
\tfraction,
}: {
\thours: string;
\tminutes: string;
\tseconds: string;
\tfraction: string;
}): number {
\tconst millis = Number(fraction.padEnd(3, "0"));
\treturn (
\t\tNumber(hours) * 3600 + Number(minutes) * 60 + Number(seconds) + millis / 1000
\t);
}

export function detectSubtitleFormat({ text }: { text: string }): SubtitleFormat {
\treturn TIMECODE.test(normalize({ text })) ? "srt" : "txt";
}

/** 解析 SRT；不是 SRT（没有时间码）时返回空数组。 */
export function parseSubtitleCues({ text }: { text: string }): SubtitleCue[] {
\tconst source = normalize({ text });
\tif (!TIMECODE.test(source)) {
\t\treturn [];
\t}

\tconst cues: SubtitleCue[] = [];
\tfor (const block of source.split(/\\n{2,}/)) {
\t\tconst lines = block
\t\t\t.split("\\n")
\t\t\t.map((line) => line.trim())
\t\t\t.filter(Boolean);
\t\tif (lines.length === 0) {
\t\t\tcontinue;
\t\t}

\t\tconst timeIndex = lines.findIndex((line) => TIMECODE.test(line));
\t\tif (timeIndex === -1) {
\t\t\tcontinue;
\t\t}
\t\tconst match = TIMECODE.exec(lines[timeIndex]);
\t\tif (!match) {
\t\t\tcontinue;
\t\t}

\t\tconst start = toSeconds({
\t\t\thours: match[1],
\t\t\tminutes: match[2],
\t\t\tseconds: match[3],
\t\t\tfraction: match[4],
\t\t});
\t\tconst end = toSeconds({
\t\t\thours: match[5],
\t\t\tminutes: match[6],
\t\t\tseconds: match[7],
\t\t\tfraction: match[8],
\t\t});
\t\tconst content = lines.slice(timeIndex + 1).join("\\n");
\t\tif (!content) {
\t\t\tcontinue;
\t\t}
\t\tcues.push({ start, end: Math.max(start + 0.2, end), text: content });
\t}

\treturn cues;
}

/** 面板上显示的条数：SRT 按条数算，TXT 按非空行数算。 */
export function countSubtitleEntries({ text }: { text: string }): number {
\tconst cues = parseSubtitleCues({ text });
\tif (cues.length > 0) {
\t\treturn cues.length;
\t}
\treturn normalize({ text })
\t\t.split("\\n")
\t\t.map((line) => line.trim())
\t\t.filter(Boolean).length;
}
'''
write(SUBTITLE_FORMAT, SUBTITLE_FORMAT_SOURCE)


IMPORT_TS = SRC / "lib" / "transcription" / "import.ts"
patch(
    IMPORT_TS,
    'import { FONT_SIZE_SCALE_REFERENCE } from "@/constants/text-constants";',
    'import { FONT_SIZE_SCALE_REFERENCE } from "@/constants/text-constants";\n'
    'import { parseSubtitleCues } from "./subtitle-format";',
)
patch(
    IMPORT_TS,
    "\tconst lines = parseCaptionLines({ text });",
    "\t// 贴进来/选进来的可能是 SRT（带时间码）或纯文本（一行一条）\n"
    "\tconst cues = parseSubtitleCues({ text });\n"
    "\tconst lines =\n"
    "\t\tcues.length > 0 ? cues.map((cue) => cue.text) : parseCaptionLines({ text });",
)
patch(
    IMPORT_TS,
    "\tlet nextStartTime = startTime;\n"
    "\tfor (const [index, line] of lines.entries()) {\n"
    "\t\tconst caption = createSubtitleFromTemplate({\n"
    "\t\t\ttemplate,\n"
    "\t\t\tstartTime: nextStartTime,\n"
    "\t\t});",
    "\tlet nextStartTime = startTime;\n"
    "\tfor (const [index, line] of lines.entries()) {\n"
    "\t\tconst cue = cues[index];\n"
    "\t\tconst caption = createSubtitleFromTemplate({\n"
    "\t\t\ttemplate,\n"
    "\t\t\t// SRT 用文件里的时间码（以播放头为 0 点），纯文本按模板时长顺排\n"
    "\t\t\tstartTime: cue ? startTime + cue.start : nextStartTime,\n"
    "\t\t});\n"
    "\t\tif (cue) {\n"
    "\t\t\tcaption.duration = Math.max(0.2, cue.end - cue.start);\n"
    "\t\t}",
)


CAPTIONS_PANEL = SRC / "components" / "editor" / "panels" / "assets" / "views" / "captions.tsx"
patch(
    CAPTIONS_PANEL,
    "import { useState, useRef, useMemo } from \"react\";",
    "import { useState, useRef, useMemo, type ChangeEvent } from \"react\";",
)
patch(
    CAPTIONS_PANEL,
    'import { parseCaptionLines } from "@/lib/transcription/import";',
    'import { countSubtitleEntries } from "@/lib/transcription/subtitle-format";',
)
patch(
    CAPTIONS_PANEL,
    "() => parseCaptionLines({ text: captionText }).length,",
    "() => countSubtitleEntries({ text: captionText }),",
)
patch(
    CAPTIONS_PANEL,
    "() => parseCaptionLines({ text: secondaryCaptionText }).length,",
    "() => countSubtitleEntries({ text: secondaryCaptionText }),",
)
patch(
    CAPTIONS_PANEL,
    "\tconst containerRef = useRef<HTMLDivElement>(null);",
    "\tconst containerRef = useRef<HTMLDivElement>(null);\n"
    "\tconst subtitleFileRef = useRef<HTMLInputElement>(null);",
)
patch(
    CAPTIONS_PANEL,
    "\tconst handleLanguageChange = ({ value }: { value: string }) => {",
    "\t/** 选中 .srt / .txt 后直接读进文本框，后面的导入流程会自动识别格式。 */\n"
    "\tconst handleSubtitleFile = async (event: ChangeEvent<HTMLInputElement>) => {\n"
    "\t\tconst file = event.target.files?.[0];\n"
    "\t\tevent.target.value = \"\";\n"
    "\t\tif (!file) return;\n"
    "\t\ttry {\n"
    "\t\t\tconst text = await file.text();\n"
    "\t\t\tsetIsEditingSecondary(false);\n"
    "\t\t\tsetCaptionText({ value: text });\n"
    "\t\t\tsetError(null);\n"
    "\t\t} catch {\n"
    "\t\t\tsetError(t(\"Failed to read the subtitle file\"));\n"
    "\t\t}\n"
    "\t};\n\n"
    "\tconst handleLanguageChange = ({ value }: { value: string }) => {",
)
patch(
    CAPTIONS_PANEL,
    '\t\t\t\t\t\t<Textarea\n\t\t\t\t\t\t\tid="caption-import-text"',
    "\t\t\t\t\t\t<div className=\"flex items-center gap-2\">\n"
    "\t\t\t\t\t\t\t<Button\n"
    "\t\t\t\t\t\t\t\ttype=\"button\"\n"
    "\t\t\t\t\t\t\t\tvariant=\"outline\"\n"
    "\t\t\t\t\t\t\t\tsize=\"sm\"\n"
    "\t\t\t\t\t\t\t\tdisabled={isProcessing}\n"
    "\t\t\t\t\t\t\t\tonClick={() => subtitleFileRef.current?.click()}\n"
    "\t\t\t\t\t\t\t>\n"
    "\t\t\t\t\t\t\t\t{t(\"Import .srt / .txt file\")}\n"
    "\t\t\t\t\t\t\t</Button>\n"
    "\t\t\t\t\t\t\t<input\n"
    "\t\t\t\t\t\t\t\tref={subtitleFileRef}\n"
    "\t\t\t\t\t\t\t\ttype=\"file\"\n"
    "\t\t\t\t\t\t\t\taccept=\".srt,.txt,text/plain\"\n"
    "\t\t\t\t\t\t\t\tclassName=\"hidden\"\n"
    "\t\t\t\t\t\t\t\tonChange={handleSubtitleFile}\n"
    "\t\t\t\t\t\t\t/>\n"
    "\t\t\t\t\t\t\t<span className=\"text-muted-foreground text-xs\">\n"
    "\t\t\t\t\t\t\t\t{t(\"SRT keeps its timing, TXT is one subtitle per line\")}\n"
    "\t\t\t\t\t\t\t</span>\n"
    "\t\t\t\t\t\t</div>\n"
    '\t\t\t\t\t\t<Textarea\n\t\t\t\t\t\t\tid="caption-import-text"',
)


# 新增字幕导入文案的中文对照（项目惯例：按当前语言直接给中文，不走翻译包）
patch(
    SUBTITLE_FORMAT,
    "/** 面板上显示的条数：SRT 按条数算，TXT 按非空行数算。 */",
    "/** 新增界面文案的中文对照（其它语言回退英文） */\n"
    "const SUBTITLE_LABELS_ZH: Record<string, string> = {\n"
    '\t"Import .srt / .txt file": "导入 .srt / .txt 文件",\n'
    '\t"SRT keeps its timing, TXT is one subtitle per line":\n'
    '\t\t"SRT 保留时间码，TXT 一行一条",\n'
    '\t"Failed to read the subtitle file": "字幕文件读取失败",\n'
    "};\n\n"
    "export function subtitleUiLabel({ key }: { key: string }): string {\n"
    "\tconst isChinese =\n"
    '\t\ttypeof window !== "undefined" &&\n'
    '\t\twindow.location.pathname.split("/").includes("zh");\n'
    "\treturn (isChinese && SUBTITLE_LABELS_ZH[key]) || key;\n"
    "}\n\n"
    "/** 面板上显示的条数：SRT 按条数算，TXT 按非空行数算。 */",
)
patch(
    CAPTIONS_PANEL,
    'import { countSubtitleEntries } from "@/lib/transcription/subtitle-format";',
    "import {\n\tcountSubtitleEntries,\n\tsubtitleUiLabel,\n} from \"@/lib/transcription/subtitle-format\";",
)
patch(
    CAPTIONS_PANEL,
    'setError(t("Failed to read the subtitle file"));',
    'setError(subtitleUiLabel({ key: "Failed to read the subtitle file" }));',
)
patch(
    CAPTIONS_PANEL,
    '{t("Import .srt / .txt file")}',
    '{subtitleUiLabel({ key: "Import .srt / .txt file" })}',
)
patch(
    CAPTIONS_PANEL,
    '{t("SRT keeps its timing, TXT is one subtitle per line")}',
    '{\n\t\t\t\t\t\t\t\t\tsubtitleUiLabel({\n'
    '\t\t\t\t\t\t\t\t\t\tkey: "SRT keeps its timing, TXT is one subtitle per line",\n'
    "\t\t\t\t\t\t\t\t\t})\n"
    "\t\t\t\t\t\t\t\t}",
)


# --------------------------------------------- 转录字幕：中文按标点断句、去标点
CAPTION_TEXT = SRC / "lib" / "transcription" / "caption-text.ts"
CAPTION_TEXT_SOURCE = '''/**
 * 转录文本切字幕（中文/日文这类没有空格的语言）。
 *
 * 关键点（都是踩过的坑）：
 *
 * - 中文按标点断句，**标点不保留**在字幕里
 * - 中文里夹英文（`这个是 Python 代码`）时，**空格不是断点**，
 *   英文单词也不会被拆开；行首行尾不会留空格
 * - 数字/时间里的标点不当句末：`1.0`、`10:30`、`3,000` 不会被切开
 * - 断点优先落在自然位置（助词后 / 连接词前），行长贴近理想值
 * - 只处理中日韩文本；英文等有空格的语言仍走上游按词切分
 */

/** 中日韩文字（含假名、谚文），判断是否走中文切分 */
const CJK_PATTERN = /[\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF]/u;

/** 中日韩标点：出现即断句，且不保留 */
const CJK_PUNCTUATION = new Set(["。", "！", "？", "；", "，", "、", "：", "…", "．", "｡", "，"]);

/** 西文标点：只有在「后面是空格/中文/结尾」时才算句末，避免切开 1.0 / 10:30 / 3,000 */
const ASCII_PUNCTUATION = new Set([".", "!", "?", ";", ",", ":"]);

/** 单条字幕最大字数（中文按字算，英文字母各占 1） */
export const MAX_CAPTION_CHARS = 16;

/** 一行至少几个字 */
const MIN_LINE_CHARS = 5;

/** 适合收尾、不适合开头的助词 */
const DEPENDENT_PREFIX = new Set([
\t"的", "地", "得", "了", "着", "过", "吗", "呢", "吧", "啊", "嘛", "呀", "哦",
]);

/** 适合开头、不适合收尾的连接词 */
const BREAK_BEFORE = new Set([
\t"然后", "所以", "但是", "不过", "而且", "因为", "如果", "就是", "我们", "你们",
\t"他们", "这个", "那个", "可以", "需要", "现在", "今天", "明天", "一下", "其实",
\t"另外", "比如", "首先", "其次", "最后", "虽然", "只是", "还是", "或者",
]);

export function countCaptionUnits({ text }: { text: string }): number {
\tconst clean = text.trim();
\tif (!clean) return 0;
\tif (CJK_PATTERN.test(clean)) {
\t\t// 标点与空格既不占语速也不占字幕时间
\t\treturn clean.replace(/\\s+/g, "").replace(/[。！？；，、：…．｡!?;:,.]+/g, "").length;
\t}
\treturn clean.split(/\\s+/).filter(Boolean).length;
}

/**
 * 按标点断句。西文标点要看上下文：`1.0`、`10:30`、`3,000` 里的不算句末。
 */
function splitClauses({ text }: { text: string }): string[] {
\tconst clauses: string[] = [];
\tlet current = "";

\tfor (let i = 0; i < text.length; i += 1) {
\t\tconst char = text[i];
\t\tconst next = text[i + 1];
\t\tconst previous = text[i - 1];

\t\tif (CJK_PUNCTUATION.has(char)) {
\t\t\tclauses.push(current);
\t\t\tcurrent = "";
\t\t\tcontinue;
\t\t}

\t\tif (ASCII_PUNCTUATION.has(char)) {
\t\t\tconst digitBefore = previous !== undefined && /\\d/.test(previous);
\t\t\tconst digitAfter = next !== undefined && /\\d/.test(next);
\t\t\tconst letterBefore = previous !== undefined && /[A-Za-z]/.test(previous);
\t\t\tconst letterAfter = next !== undefined && /[A-Za-z]/.test(next);
\t\t\t// 数字内（1.0 / 10:30 / 3,000）、缩写（e.g / U.S）都不算句末
\t\t\tconst insideNumber =
\t\t\t\t(digitBefore && digitAfter) ||
\t\t\t\t(char === ":" && digitBefore && digitAfter) ||
\t\t\t\t(char === "." && digitBefore && digitAfter);
\t\t\tconst insideAbbreviation = char === "." && letterBefore && letterAfter;
\t\t\tconst followedByBoundary =
\t\t\t\tnext === undefined || /\\s/.test(next) || CJK_PATTERN.test(next);
\t\t\tif (!insideNumber && !insideAbbreviation && followedByBoundary) {
\t\t\t\tclauses.push(current);
\t\t\t\tcurrent = "";
\t\t\t\tcontinue;
\t\t\t}
\t\t}

\t\tcurrent += char;
\t}
\tclauses.push(current);
\treturn clauses.map((clause) => clause.replace(/\\s+/g, " ").trim()).filter(Boolean);
}

/**
 * 分词：中日韩走 ICU 词典；空格**不作为独立片段**，而是粘在前一个词后面，
 * 这样断行永远不会落在空格上，行首行尾也不会留空格。
 */
function splitWords({ text }: { text: string }): string[] {
\tlet raw: string[];
\ttry {
\t\tconst segmenter = new Intl.Segmenter("zh", { granularity: "word" });
\t\traw = Array.from(segmenter.segment(text), (item) => item.segment);
\t} catch {
\t\traw = Array.from(text);
\t}

\tconst words: string[] = [];
\tfor (const piece of raw) {
\t\tif (!piece) continue;
\t\tif (piece.trim().length === 0) {
\t\t\t// 空白：粘到前一个词上，避免成为断点
\t\t\tif (words.length > 0) {
\t\t\t\twords[words.length - 1] = `${words[words.length - 1]}${piece}`;
\t\t\t} else {
\t\t\t\twords.push(piece);
\t\t\t}
\t\t\tcontinue;
\t\t}
\t\twords.push(piece);
\t}
\treturn words;
}

/** 这个断点够不够自然（不做空格断点：空格已经粘进词里） */
function isNaturalBoundary({
\tprevious,
\tnext,
}: {
\tprevious: string | undefined;
\tnext: string | undefined;
}): boolean {
\tif (!previous || !next) return false;
\tif (previous.trim() !== previous) return false;
\tif (next.trim() !== next) return false;
\tif (previous.length === 1 && !DEPENDENT_PREFIX.has(previous)) return false;
\tif (BREAK_BEFORE.has(previous)) return false;
\tif (DEPENDENT_PREFIX.has(next)) return false;
\treturn true;
}

/** 把词装箱成行：不切词、不在空格处断行，行长贴近理想值 */
function packWords({ words, limit }: { words: string[]; limit: number }): string[][] {
\tconst groups: string[][] = [];
\tconst ideal = Math.max(MIN_LINE_CHARS, Math.round(limit * 0.8));
\tlet index = 0;

\twhile (index < words.length) {
\t\tlet length = 0;
\t\tlet end = index;
\t\tlet bestCut = -1;
\t\tlet bestGap = Number.POSITIVE_INFINITY;

\t\twhile (end < words.length && length + words[end].length <= limit) {
\t\t\tconst piece = words[end].trimEnd();
\t\t\tlength += words[end].length;
\t\t\tconst cut = end + 1;
\t\t\tif (
\t\t\t\tcut < words.length &&
\t\t\t\tlength >= MIN_LINE_CHARS &&
\t\t\t\tisNaturalBoundary({ previous: piece, next: words[cut].trimStart() })
\t\t\t) {
\t\t\t\tconst gap = Math.abs(length - ideal);
\t\t\t\tif (gap < bestGap) {
\t\t\t\t\tbestGap = gap;
\t\t\t\t\tbestCut = cut;
\t\t\t\t}
\t\t\t}
\t\t\tend += 1;
\t\t}

\t\tif (end === index) {
\t\t\t// 单个词就超上限：英文单词/链接不硬拆，实在过长才切
\t\t\tconst word = words[index].trimEnd();
\t\t\tif (word.length <= limit * 2) {
\t\t\t\tgroups.push([word]);
\t\t\t} else {
\t\t\t\tfor (let i = 0; i < word.length; i += limit) {
\t\t\t\t\tgroups.push([word.slice(i, i + limit)]);
\t\t\t\t}
\t\t\t}
\t\t\tindex += 1;
\t\t\tcontinue;
\t\t}

\t\tif (end >= words.length) {
\t\t\tgroups.push(words.slice(index, end));
\t\t\tbreak;
\t\t}

\t\tif (bestCut < 0) {
\t\t\tlet acc = 0;
\t\t\tlet gap = Number.POSITIVE_INFINITY;
\t\t\tfor (let i = index; i < end; i += 1) {
\t\t\t\tacc += words[i].trimEnd().length;
\t\t\t\tconst candidateGap = Math.abs(acc - ideal);
\t\t\t\tif (acc >= MIN_LINE_CHARS && candidateGap < gap) {
\t\t\t\t\tgap = candidateGap;
\t\t\t\t\tbestCut = i + 1;
\t\t\t\t}
\t\t\t}
\t\t}
\t\tif (bestCut < 0) bestCut = end;

\t\tgroups.push(words.slice(index, bestCut));
\t\tindex = bestCut;
\t}

\treturn smoothBoundaries({ groups, limit });
}

/** 兜底：换行处仍不自然时，把上一行最后一个词挪到下一行行首 */
function smoothBoundaries({
\tgroups,
\tlimit,
}: {
\tgroups: string[][];
\tlimit: number;
}): string[][] {
\tfor (let i = 0; i + 1 < groups.length; i += 1) {
\t\tconst current = groups[i];
\t\tconst next = groups[i + 1];
\t\tif (current.length <= 1 || next.length === 0) continue;
\t\tconst rawWord = current[current.length - 1];
\t\tconst lastWord = rawWord.trimEnd();
\t\tif (isNaturalBoundary({ previous: lastWord, next: next[0].trimStart() })) continue;
\t\tif (current.join("").trimEnd().length - lastWord.length < MIN_LINE_CHARS) continue;
\t\tif (next.join("").trimEnd().length + rawWord.length > limit) continue;
\t\tcurrent.pop();
\t\tnext.unshift(rawWord);
\t}
\treturn groups;
}

/** 尾部只剩 1~2 字时，从上一行挪词过来 */
function fixShortTail({ groups }: { groups: string[][] }): string[][] {
\tconst minChars = 3;
\twhile (groups.length > 1) {
\t\tconst last = groups[groups.length - 1];
\t\tconst previous = groups[groups.length - 2];
\t\tif (last.join("").trim().length >= minChars || previous.length <= 1) break;
\t\tlast.unshift(previous.pop() as string);
\t}
\treturn groups;
}

/** 只有 1~2 字的碎句（"哦""好的"）并回上一条 */
function mergeTinyClauses({
\tgroups,
\tlimit,
}: {
\tgroups: string[][];
\tlimit: number;
}): string[][] {
\tconst merged: string[][] = [];
\tfor (const group of groups) {
\t\tconst size = group.join("").trim().length;
\t\tconst last = merged[merged.length - 1];
\t\tif (last && size <= 2 && last.join("").trim().length + size <= limit + 2) {
\t\t\tlast.push(...group);
\t\t\tcontinue;
\t\t}
\t\tmerged.push([...group]);
\t}
\treturn merged;
}

/**
 * 把一段转录文本切成若干条字幕。
 *
 * 不是中日韩文本时返回空数组，调用方继续用上游的「按词切」逻辑。
 */
export function splitCaptionText({
\ttext,
\tmaxChars = MAX_CAPTION_CHARS,
}: {
\ttext: string;
\tmaxChars?: number;
}): string[] {
\tconst clean = text.replace(/\\s+/g, " ").trim();
\tif (!clean || !CJK_PATTERN.test(clean)) return [];

\tconst limit = Math.max(8, Math.floor(maxChars));
\tconst groups: string[][] = [];
\tfor (const clause of splitClauses({ text: clean })) {
\t\tconst words = splitWords({ text: clause });
\t\tif (words.length === 0) continue;
\t\tgroups.push(...fixShortTail({ groups: packWords({ words, limit }) }));
\t}

\treturn mergeTinyClauses({ groups, limit })
\t\t.map((group) => group.join("").replace(/\\s+/g, " ").trim())
\t\t.filter(Boolean);
}
'''
write(CAPTION_TEXT, CAPTION_TEXT_SOURCE)


CAPTION_LIB = SRC / "lib" / "transcription" / "caption.ts"
patch(
    CAPTION_LIB,
    'import {\n\tDEFAULT_WORDS_PER_CAPTION,\n\tMIN_CAPTION_DURATION_SECONDS,\n} from "@/constants/transcription-constants";',
    'import {\n\tDEFAULT_WORDS_PER_CAPTION,\n\tMIN_CAPTION_DURATION_SECONDS,\n} from "@/constants/transcription-constants";\n'
    'import { countCaptionUnits, splitCaptionText } from "./caption-text";',
)
patch(
    CAPTION_LIB,
    "\t\tconst wordsPerSecond = words.length / segmentDuration;",
    "\t\t// 中日韩按字算语速，英语按词算\n"
    "\t\tconst unitsPerSecond = Math.max(\n"
    "\t\t\t0.001,\n"
    "\t\t\tcountCaptionUnits({ text }) / segmentDuration,\n"
    "\t\t);",
)
patch(
    CAPTION_LIB,
    "\t\tconst chunks: string[] = [];\n"
    "\t\tfor (let i = 0; i < words.length; i += wordsPerChunk) {\n"
    "\t\t\tchunks.push(words.slice(i, i + wordsPerChunk).join(\" \"));\n"
    "\t\t}",
    "\t\t// 中文没有空格：按标点断句、每行限字（标点不保留）\n"
    "\t\tconst chunks: string[] = splitCaptionText({ text });\n"
    "\t\tif (chunks.length === 0) {\n"
    "\t\t\tfor (let i = 0; i < words.length; i += wordsPerChunk) {\n"
    "\t\t\t\tchunks.push(words.slice(i, i + wordsPerChunk).join(\" \"));\n"
    "\t\t\t}\n"
    "\t\t}",
)
patch(
    CAPTION_LIB,
    "\t\t\tconst chunkWords = chunk.split(/\\s+/).length;",
    "\t\t\tconst chunkUnits = Math.max(1, countCaptionUnits({ text: chunk }));",
)
patch(
    CAPTION_LIB,
    "\t\t\t\tMath.max(minDuration, chunkWords / wordsPerSecond),",
    "\t\t\t\tMath.max(minDuration, chunkUnits / unitsPerSecond),",
)



# ------------------------------------ 视频片段：名称/时长 + 画面 + 半波形三段式
# 波形直接复用音频轨道那套（AudioWaveform / wavesurfer），只是把它按 2 倍高度渲染、
# 再裁掉下半部分——这样「只显示上半边」，颜色/粗细/加载态都和音频轨道完全一致。
TIMELINE_ELEMENT = SRC / "components" / "editor" / "panels" / "timeline" / "timeline-element.tsx"
patch(
    TIMELINE_ELEMENT,
    '''\t\treturn (
\t\t\t<div className="relative size-full">
\t\t\t\t<VideoThumbnailStrip''',
    '''\t\t// 上 1/4：名称 + 时长；中 1/2：画面；下 1/4：波形上半边
\t\tconst bandHeight = Math.max(8, Math.round(trackHeight / 4));
\t\treturn (
\t\t\t<div className="flex size-full flex-col">
\t\t\t\t<div
\t\t\t\t\tclassName="flex shrink-0 items-center gap-1 overflow-hidden px-1.5"
\t\t\t\t\tstyle={{ height: bandHeight }}
\t\t\t\t>
\t\t\t\t\t<span className="truncate text-[10px] leading-none text-white/90">
\t\t\t\t\t\t{element.name}
\t\t\t\t\t</span>
\t\t\t\t\t<span className="ml-auto shrink-0 text-[10px] leading-none tabular-nums text-white/60">
\t\t\t\t\t\t{element.duration.toFixed(1)}s
\t\t\t\t\t</span>
\t\t\t\t</div>
\t\t\t\t<div className="relative min-h-0 flex-1 overflow-hidden">
\t\t\t\t<VideoThumbnailStrip''',
)
patch(
    TIMELINE_ELEMENT,
    '''\t\t\t\t\ttrackHeight={trackHeight}
\t\t\t\t\tzoomLevel={zoomLevel}
\t\t\t\t\tfps={mediaAsset.fps ?? 30}
\t\t\t\t\tmediaWidth={mediaAsset.width ?? 1920}
\t\t\t\t\tmediaHeight={mediaAsset.height ?? 1080}
\t\t\t\t/>
\t\t\t</div>
\t\t);''',
    '''\t\t\t\t\ttrackHeight={trackHeight - bandHeight * 2}
\t\t\t\t\tzoomLevel={zoomLevel}
\t\t\t\t\tfps={mediaAsset.fps ?? 30}
\t\t\t\t\tmediaWidth={mediaAsset.width ?? 1920}
\t\t\t\t\tmediaHeight={mediaAsset.height ?? 1080}
\t\t\t\t/>
\t\t\t\t</div>
\t\t\t\t{/* 波形按 2 倍高度渲染，容器只露上半部分 → 只显示上半边 */}
\t\t\t\t<div className="relative shrink-0 overflow-hidden" style={{ height: bandHeight }}>
\t\t\t\t\t<div className="absolute inset-x-0 top-0" style={{ height: bandHeight * 2 }}>
\t\t\t\t\t\t<AudioWaveform
\t\t\t\t\t\t\taudioBlob={mediaAsset.file}
\t\t\t\t\t\t\tduration={element.duration}
\t\t\t\t\t\t\theight={bandHeight * 2}
\t\t\t\t\t\t\tclassName="w-full"
\t\t\t\t\t\t/>
\t\t\t\t\t</div>
\t\t\t\t</div>
\t\t\t</div>
\t\t);''',
)


# --------------------------------- 视频素材到底带不带音轨（不解码，只解封装）
AUDIO_PROBE = SRC / "lib" / "media" / "audio-probe.ts"
AUDIO_PROBE.write_text(
    '''import { ALL_FORMATS, BlobSource, Input } from "mediabunny";
import type { MediaAsset } from "@/types/assets";

// 只解封装、不解码，几十毫秒；结果按素材 id 缓存，重复渲染不会反复探测。
const cache = new Map<string, Promise<boolean>>();

async function detect({ media }: { media: MediaAsset }): Promise<boolean> {
\tif (media.type === "audio") return true;
\tif (media.type !== "video" || !media.file) return false;
\ttry {
\t\tconst input = new Input({
\t\t\tsource: new BlobSource(media.file),
\t\t\tformats: ALL_FORMATS,
\t\t});
\t\tconst track = await input.getPrimaryAudioTrack();
\t\treturn Boolean(track);
\t} catch {
\t\t// 探测失败别把功能藏起来，按「有音频」处理
\t\treturn true;
\t}
}

export function probeMediaHasAudio({
\tmedia,
}: {
\tmedia: MediaAsset | null | undefined;
}): Promise<boolean> | null {
\tif (!media) return null;
\tconst cached = cache.get(media.id);
\tif (cached) return cached;
\tconst pending = detect({ media });
\tcache.set(media.id, pending);
\treturn pending;
}
''',
    encoding="utf-8",
)
print(f"  wrote {AUDIO_PROBE.relative_to(ROOT)} (音轨探测)")


# ------------------------------- 视频属性面板：人声美化（勾选才处理）
VIDEO_PANEL = VOICE_PANEL_DIR / "video-properties.tsx"
_vp = VIDEO_PANEL.read_text(encoding="utf-8")

VIDEO_HANDLER = '''\tconst localAsset =
\t\telement.type === "video"
\t\t\t? (editor.media.getAssets().find((asset) => asset.id === element.mediaId) ??
\t\t\t\tnull)
\t\t\t: null;
\tconst [beautifyBusy, setBeautifyBusy] = useState(false);
\tconst [beautifyStatus, setBeautifyStatus] = useState("");
\tconst [beautifyStrength, setBeautifyStrength] = useState(
\t\t(element as VideoElement).voiceBeautifyStrength ?? 0.8,
\t);
\tconst [assetHasAudio, setAssetHasAudio] = useState<boolean | null>(null);
\tconst [beautifyProgress, setBeautifyProgress] = useState<number | null>(null);
\t// 点击后先用本地状态把勾打上（不等处理完），等片段字段生效再交回去
\tconst [beautifyOverride, setBeautifyOverride] = useState<boolean | null>(null);

\tconst probeAssetId = element.type === "video" ? element.mediaId : "";
\tuseEffect(() => {
\t\tsetAssetHasAudio(null);
\t\tif (!probeAssetId) return;
\t\tconst media =
\t\t\teditor.media.getAssets().find((asset) => asset.id === probeAssetId) ?? null;
\t\tconst pending = probeMediaHasAudio({ media });
\t\tif (!pending) return;
\t\tlet cancelled = false;
\t\tvoid pending.then((hasAudio) => {
\t\t\tif (!cancelled) setAssetHasAudio(hasAudio);
\t\t});
\t\treturn () => {
\t\t\tcancelled = true;
\t\t};
\t}, [editor, probeAssetId]);

\tconst beautifyStoredEnabled =
\t\telement.type === "video" && element.voiceBeautifyEnabled === true;
\tconst beautifyEnabled = beautifyOverride ?? beautifyStoredEnabled;

\tuseEffect(() => {
\t\tif (beautifyOverride !== null && beautifyOverride === beautifyStoredEnabled) {
\t\t\tsetBeautifyOverride(null);
\t\t}
\t}, [beautifyOverride, beautifyStoredEnabled]);

\tconst updateClip = (updates: Partial<Record<string, unknown>>) => {
\t\teditor.timeline.updateElements({
\t\t\tupdates: [{ trackId, elementId: element.id, updates }],
\t\t\tpushHistory: true,
\t\t});
\t};

\tconst showBeautifyRow =
\t\telement.type === "video" &&
\t\t!element.muted &&
\t\tassetHasAudio === true &&
\t\tBoolean(localAsset?.file);

\tconst runVideoBeautify = async () => {
\t\tif (element.type !== "video" || beautifyBusy) return;
\t\tconst source = editor.media
\t\t\t.getAssets()
\t\t\t.find((asset) => asset.id === element.mediaId);
\t\tif (!source) {
\t\t\tsetBeautifyOverride(null);
\t\t\tsetBeautifyStatus("找不到原始素材");
\t\t\treturn;
\t\t}
\t\tsetBeautifyBusy(true);
\t\tsetBeautifyProgress(0);
\t\tsetBeautifyStatus("准备中…");
\t\ttry {
\t\t\tconst result = await applyVocalBeautify({
\t\t\t\teditor,
\t\t\t\tsource,
\t\t\t\tstrength: beautifyStrength,
\t\t\t\tonProgress: (state) => {
\t\t\t\t\tsetBeautifyStatus(describeVocalBeautify(state));
\t\t\t\t\tsetBeautifyProgress(state.progress ?? 0);
\t\t\t\t},
\t\t\t});
\t\t\tupdateClip({
\t\t\t\tvoiceBeautifyEnabled: true,
\t\t\t\tvoiceBeautifyMediaId: result.mediaId,
\t\t\t\tvoiceBeautifyStrength: beautifyStrength,
\t\t\t});
\t\t\tsetBeautifyStatus("已处理：画面不变，声音换成处理后的音轨");
\t\t} catch (error) {
\t\t\tsetBeautifyOverride(null);
\t\t\tsetBeautifyStatus(`失败：${(error as Error).message}`);
\t\t} finally {
\t\t\tsetBeautifyBusy(false);
\t\t\tsetBeautifyProgress(null);
\t\t}
\t};

\tconst toggleVideoBeautify = (checked: boolean) => {
\t\tif (element.type !== "video" || beautifyBusy) return;
\t\tsetBeautifyOverride(checked);
\t\tif (!checked) {
\t\t\tupdateClip({ voiceBeautifyEnabled: false });
\t\t\tsetBeautifyStatus("");
\t\t\treturn;
\t\t}
\t\tif (element.voiceBeautifyMediaId) {
\t\t\tupdateClip({ voiceBeautifyEnabled: true });
\t\t\tsetBeautifyStatus("");
\t\t\treturn;
\t\t}
\t\tvoid runVideoBeautify();
\t};

'''

VIDEO_CARD = '''\t\t\t\t{element.type === "video" && showBeautifyRow ? (
\t\t\t\t\t<VocalBeautifyRow
\t\t\t\t\t\tenabled={beautifyEnabled}
\t\t\t\t\t\tbusy={beautifyBusy}
\t\t\t\t\t\tstrength={beautifyStrength}
\t\t\t\t\t\tstatus={beautifyStatus}
\t\t\t\t\t\tprogress={beautifyProgress}
\t\t\t\t\t\tneedsApply={
\t\t\t\t\t\t\tbeautifyEnabled &&
\t\t\t\t\t\t\tMath.abs(
\t\t\t\t\t\t\t\t(element.voiceBeautifyStrength ?? -1) - beautifyStrength,
\t\t\t\t\t\t\t) > 0.001
\t\t\t\t\t\t}
\t\t\t\t\t\tonToggle={toggleVideoBeautify}
\t\t\t\t\t\tonStrengthChange={(value) => {
\t\t\t\t\t\t\tsetBeautifyStrength(clamp({ value, min: 0, max: 1 }));
\t\t\t\t\t\t}}
\t\t\t\t\t\tonApply={() => {
\t\t\t\t\t\t\tvoid runVideoBeautify();
\t\t\t\t\t\t}}
\t\t\t\t\t/>
\t\t\t\t) : null}

'''

if "VocalBeautifyRow" not in _vp:
    _vp = _vp.replace(
        'import { useReducer, useRef } from "react";',
        'import { useEffect, useReducer, useRef, useState } from "react";',
        1,
    )
    _vp = _vp.replace(
        'import { formatSpeedLabel } from "@/lib/timeline/speed-utils";',
        'import { formatSpeedLabel } from "@/lib/timeline/speed-utils";\n'
        'import { probeMediaHasAudio } from "@/lib/media/audio-probe";\n'
        'import {\n\tapplyVocalBeautify,\n\tdescribeVocalBeautify,\n} from "@/lib/audio/vocal-beautify";\n'
        'import { VocalBeautifyRow } from "./vocal-beautify-row";',
        1,
    )
    _vp = _vp.replace(
        "\tconst [, forceRender] = useReducer((x: number) => x + 1, 0);",
        "\tconst [, forceRender] = useReducer((x: number) => x + 1, 0);\n" + VIDEO_HANDLER,
        1,
    )
    _vp_marker = "\t\t\t\t</PanelBaseView>"
    _vp_index = _vp.rfind(_vp_marker)
    if _vp_index == -1:
        raise SystemExit(f"panel view marker not found in {VIDEO_PANEL}")
    _vp = _vp[:_vp_index] + VIDEO_CARD + _vp[_vp_index:]
    VIDEO_PANEL.write_text(_vp, encoding="utf-8")
    print(f"  patched {VIDEO_PANEL.relative_to(ROOT)} (视频人声美化)")


# ------------------------- 视频片段：分离音频（muted）后不再画波形
TIMELINE_ELEMENT = SRC / "components" / "editor" / "panels" / "timeline" / "timeline-element.tsx"
_te = TIMELINE_ELEMENT.read_text(encoding="utf-8")

if "beautifiedFile" not in _te:
    _te = _te.replace(
        '\tif (mediaAsset.type === "video" && mediaAsset.file) {\n'
        "\t\tconst trackHeight = getTrackHeight({ type: track.type });",
        '\tif (element.type === "video" && mediaAsset.type === "video" && mediaAsset.file) {\n'
        "\t\t// 勾了人声美化时，波形画处理后的音频，跟听到的一致\n"
        "\t\tconst beautifiedAsset =\n"
        "\t\t\telement.voiceBeautifyEnabled && element.voiceBeautifyMediaId\n"
        "\t\t\t\t? mediaAssets.find((asset) => asset.id === element.voiceBeautifyMediaId)\n"
        "\t\t\t\t: undefined;\n"
        "\t\tconst beautifiedFile = beautifiedAsset?.file ?? null;\n"
        "\t\tconst trackHeight = getTrackHeight({ type: track.type });",
        1,
    )
    _te = _te.replace(
        "\t\t\t\t{/* 波形按 2 倍高度渲染，容器只露上半部分 → 只显示上半边 */}\n"
        '\t\t\t\t<div className="relative shrink-0 overflow-hidden" style={{ height: bandHeight }}>\n'
        '\t\t\t\t\t<div className="absolute inset-x-0 top-0" style={{ height: bandHeight * 2 }}>\n'
        "\t\t\t\t\t\t<AudioWaveform\n"
        "\t\t\t\t\t\t\taudioBlob={mediaAsset.file}",
        "\t\t\t\t{/* 波形按 2 倍高度渲染，容器只露上半部分；分离音频 / 静音后不画 */}\n"
        '\t\t\t\t<div className="relative shrink-0 overflow-hidden" style={{ height: bandHeight }}>\n'
        "\t\t\t\t\t{element.muted ? (\n"
        '\t\t\t\t\t\t<span className="text-muted-foreground/70 flex h-full items-start pl-1.5 text-[9px] leading-tight">\n'
        "\t\t\t\t\t\t\t静音\n"
        "\t\t\t\t\t\t</span>\n"
        "\t\t\t\t\t) : (\n"
        '\t\t\t\t\t\t<div className="absolute inset-x-0 top-0" style={{ height: bandHeight * 2 }}>\n'
        "\t\t\t\t\t\t\t<AudioWaveform\n"
        "\t\t\t\t\t\t\t\taudioBlob={beautifiedFile ?? mediaAsset.file}",
        1,
    )
    _te = _te.replace(
        "\t\t\t\t\t\t\tduration={element.duration}\n"
        "\t\t\t\t\t\t\theight={bandHeight * 2}\n"
        '\t\t\t\t\t\t\tclassName="w-full"\n'
        "\t\t\t\t\t\t/>\n"
        "\t\t\t\t\t</div>\n"
        "\t\t\t\t</div>",
        "\t\t\t\t\t\t\t\tduration={element.duration}\n"
        "\t\t\t\t\t\t\t\theight={bandHeight * 2}\n"
        '\t\t\t\t\t\t\t\tclassName="w-full"\n'
        "\t\t\t\t\t\t\t/>\n"
        "\t\t\t\t\t\t</div>\n"
        "\t\t\t\t\t)}\n"
        "\t\t\t\t</div>",
        1,
    )
    if (
        "beautifiedFile" not in _te
        or 'element.type === "video" && mediaAsset.type === "video"' not in _te
        or "element.muted ? (" not in _te
    ):
        raise SystemExit(f"波形补丁没打上：{TIMELINE_ELEMENT}")
    TIMELINE_ELEMENT.write_text(_te, encoding="utf-8")
    print(f"  patched {TIMELINE_ELEMENT.relative_to(ROOT)} (分离音频后不画波形)")


# ------------------- 预览/客户端导出：视频勾了人声美化就用处理后的音频
AUDIO_LIB = SRC / "lib" / "media" / "audio.ts"
_al = AUDIO_LIB.read_text(encoding="utf-8")

if "resolveElementAudioAsset" not in _al:
    _al = _al.replace(
        "function collectMediaAudioClip({",
        """/**
 * 元素实际要出声的素材：视频勾了人声美化时，用处理后的 wav 取代画面自带音轨。
 */
export function resolveElementAudioAsset({
\telement,
\tmediaMap,
}: {
\telement: {
\t\tmediaId?: string;
\t\ttype?: string;
\t\tvoiceBeautifyEnabled?: boolean;
\t\tvoiceBeautifyMediaId?: string;
\t};
\tmediaMap: Map<string, MediaAsset>;
}): MediaAsset | undefined {
\tconst mediaAsset = element.mediaId ? mediaMap.get(element.mediaId) : undefined;
\tif (!mediaAsset) return undefined;
\tif (element.type === "video" && element.voiceBeautifyEnabled) {
\t\tconst beautified = element.voiceBeautifyMediaId
\t\t\t? mediaMap.get(element.voiceBeautifyMediaId)
\t\t\t: undefined;
\t\tif (beautified) return beautified;
\t}
\treturn mediaAsset;
}

function collectMediaAudioClip({""",
        1,
    )
    _video_old = (
        '\t\t\tif (element.type === "video") {\n'
        "\t\t\t\tconst mediaAsset = mediaMap.get(element.mediaId);\n"
        "\t\t\t\tif (!mediaAsset) continue;"
    )
    _video_new = (
        '\t\t\tif (element.type === "video") {\n'
        "\t\t\t\tconst mediaAsset = resolveElementAudioAsset({ element, mediaMap });\n"
        "\t\t\t\tif (!mediaAsset) continue;"
    )
    _al = _al.replace(_video_old, _video_new)
    if _al.count("resolveElementAudioAsset({ element, mediaMap })") != 2:
        raise SystemExit(f"视频音轨替换没打全：{AUDIO_LIB}")
    AUDIO_LIB.write_text(_al, encoding="utf-8")
    print(f"  patched {AUDIO_LIB.relative_to(ROOT)} (视频用人声美化后的音轨)")


# ------------------------------ 预览：原生 <video> 播放 + DOM 字幕/贴纸悬浮层
# 原来的预览是「每帧把 1080p 画面 drawImage 进 canvas」的实时合成：
# 视频解码一帧 → 拷进 canvas → 再交给合成器，GPU/CPU 都在跑满。
# 现在：视频/图片用原生 <video>/<img> 按变换摆好，浏览器自己播；
# 字幕仍用 TextNode 绘制（像素与原来一致），但只画一张静态位图、内容变了才重画；
# 贴纸是 <img>。等于「播放器照常播放，字幕浮在上面」。
PREVIEW_DOM_LAYERS = (
    SRC / "components" / "editor" / "panels" / "preview" / "preview-dom-layers.tsx"
)
PREVIEW_DOM_LAYERS_SOURCE = tabsify(
    """
"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useEditor } from "@/hooks/use-editor";

import { getVisualSourceTime } from "@/lib/timeline/element-utils";
import { isBottomAlignedSubtitleText } from "@/lib/timeline/text-utils";
import { DEFAULT_BLUR_INTENSITY } from "@/constants/project-constants";
import type { CanvasRenderer } from "@/services/renderer/canvas-renderer";
import { TextNode } from "@/services/renderer/nodes/text-node";
import type { MediaAsset } from "@/types/assets";
import type { TBackground, TCanvasSize } from "@/types/project";
import type {
  ImageElement,
  StickerElement,
  TextElement,
  TimelineElement,
  TimelineTrack,
  VideoElement,
} from "@/types/timeline";

const EPSILON = 1 / 1000;

type VisualElement = VideoElement | ImageElement;

type ActiveElements = {
  visuals: VisualElement[];
  stickers: StickerElement[];
  texts: TextElement[];
};

function isActive({
  element,
  time,
}: {
  element: TimelineElement;
  time: number;
}): boolean {
  return (
    time >= element.startTime - EPSILON &&
    time < element.startTime + element.duration - EPSILON
  );
}

function collectActive({
  tracks,
  time,
}: {
  tracks: TimelineTrack[];
  time: number;
}): ActiveElements {
  const visuals: VisualElement[] = [];
  const stickers: StickerElement[] = [];
  const texts: TextElement[] = [];

  for (const track of tracks) {
    if ("hidden" in track && track.hidden) continue;
    for (const element of track.elements) {
      if ("hidden" in element && element.hidden) continue;
      if (!isActive({ element, time })) continue;
      if (element.type === "video" || element.type === "image") {
        visuals.push(element);
      }
      if (element.type === "sticker") stickers.push(element);
      if (element.type === "text") texts.push(element);
    }
  }

  return { visuals, stickers, texts };
}

/** 和 VisualNode.renderVisual 同一套算法：contain 缩放后再乘 transform.scale。 */
function visualBox({
  element,
  sourceWidth,
  sourceHeight,
  canvasSize,
  fitCanvasSize,
}: {
  element: VisualElement | StickerElement;
  sourceWidth: number;
  sourceHeight: number;
  canvasSize: TCanvasSize;
  fitCanvasSize: TCanvasSize;
}) {
  const containScale = Math.min(
    fitCanvasSize.width / sourceWidth,
    fitCanvasSize.height / sourceHeight,
  );
  const width = sourceWidth * containScale * element.transform.scale;
  const height = sourceHeight * containScale * element.transform.scale;
  return {
    left: canvasSize.width / 2 + element.transform.position.x - width / 2,
    top: canvasSize.height / 2 + element.transform.position.y - height / 2,
    width,
    height,
  };
}

/** 画布单位 → 屏幕像素：直接按显示尺寸排版，避免多一张 1080p 缩放层。 */
function layerStyle({
  element,
  box,
  scale,
}: {
  element: VisualElement | StickerElement;
  box: { left: number; top: number; width: number; height: number };
  scale: number;
}) {
  const flipX = element.transform.flipX ? -1 : 1;
  const flipY = element.transform.flipY ? -1 : 1;
  const rotate = element.transform.rotate ?? 0;
  return {
    position: "absolute" as const,
    left: box.left * scale,
    top: box.top * scale,
    width: box.width * scale,
    height: box.height * scale,
    opacity: element.opacity,
    transform: `rotate(${rotate}deg) scale(${flipX}, ${flipY})`,
    transformOrigin: "center",
    pointerEvents: "none" as const,
  };
}

function VideoLayer({
  element,
  media,
  canvasSize,
  fitCanvasSize,
  scale,
  registerVideo,
}: {
  element: VideoElement;
  media: MediaAsset;
  canvasSize: TCanvasSize;
  fitCanvasSize: TCanvasSize;
  scale: number;
  registerVideo: (args: {
    id: string;
    element: VideoElement;
    video: HTMLVideoElement | null;
  }) => void;
}) {
  const editor = useEditor();
  const videoRef = useRef<HTMLVideoElement>(null);
  const rate = element.playbackRate ?? 1;
  const reversed = element.reversed ?? false;
  const box = visualBox({
    element,
    sourceWidth: media.width ?? canvasSize.width,
    sourceHeight: media.height ?? canvasSize.height,
    canvasSize,
    fitCanvasSize,
  });

  // 登记这个视频：播放时钟以它为准，字幕就不会跟着卡住
  useEffect(() => {
    const video = videoRef.current;
    registerVideo({ id: element.id, element, video });
    if (!video) return;
    const notify = () => {
      window.dispatchEvent(new CustomEvent("playback-update"));
    };
    const events = [
      "timeupdate",
      "seeking",
      "seeked",
      "play",
      "pause",
      "ratechange",
    ];
    for (const name of events) video.addEventListener(name, notify);
    return () => {
      for (const name of events) video.removeEventListener(name, notify);
      registerVideo({ id: element.id, element, video: null });
    };
  }, [element, registerVideo]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    let frame = 0;
    let lastAppTime = -1;

    // 浏览器自己解码播放，这里只做「对表」：跑偏了才校正，不逐帧搬图。
    const tick = () => {
      const playing = editor.playback.getIsPlaying();
      const appTime = editor.playback.getCurrentTime();
      // app 时钟是 rAF 推的，被浏览器限流时会原地不动；
      // 那种情况下不要把视频拽回去，让它自己播。
      const appAdvancing = Math.abs(appTime - lastAppTime) > 1e-4;
      lastAppTime = appTime;

      const target = getVisualSourceTime({
        timelineTime: appTime,
        startTime: element.startTime,
        duration: element.duration,
        trimStart: element.trimStart,
        playbackRate: rate,
        reversed,
      });
      const mediaDuration = Number.isFinite(video.duration) ? video.duration : 0;
      const clamped =
        mediaDuration > 0
          ? Math.min(Math.max(target, 0), Math.max(mediaDuration - 0.05, 0))
          : Math.max(target, 0);

      if (Math.abs(video.playbackRate - rate) > 0.001) {
        video.playbackRate = rate;
      }

      if (playing && !reversed) {
        if (video.paused) void video.play().catch(() => {});
        if (appAdvancing && Math.abs(video.currentTime - clamped) > 0.35) {
          video.currentTime = clamped;
        }
      } else {
        if (!video.paused) video.pause();
        if (Math.abs(video.currentTime - clamped) > 0.02) {
          video.currentTime = clamped;
        }
      }

      frame = requestAnimationFrame(tick);
    };

    tick();
    return () => cancelAnimationFrame(frame);
  }, [
    editor,
    element.startTime,
    element.duration,
    element.trimStart,
    rate,
    reversed,
  ]);

  return (
    // 静音：声音走 AudioManager（Sonic 变速不变调那条链路），这里只出画面
    <video
      ref={videoRef}
      src={media.url}
      muted
      playsInline
      preload="auto"
      disablePictureInPicture
      style={{ ...layerStyle({ element, box, scale }), objectFit: "fill" }}
    />
  );
}

function ImageLayer({
  element,
  media,
  canvasSize,
  fitCanvasSize,
  scale,
}: {
  element: ImageElement;
  media: MediaAsset;
  canvasSize: TCanvasSize;
  fitCanvasSize: TCanvasSize;
  scale: number;
}) {
  const box = visualBox({
    element,
    sourceWidth: media.width ?? canvasSize.width,
    sourceHeight: media.height ?? canvasSize.height,
    canvasSize,
    fitCanvasSize,
  });

  // biome-ignore lint/performance/noImgElement: 预览层直接用 blob URL
  return (
    <img
      src={media.url}
      alt={media.name}
      draggable={false}
      style={{ ...layerStyle({ element, box, scale }), objectFit: "fill" }}
    />
  );
}

function StickerLayer({
  element,
  canvasSize,
  scale,
}: {
  element: StickerElement;
  canvasSize: TCanvasSize;
  scale: number;
}) {
  const box = visualBox({
    element,
    sourceWidth: 200,
    sourceHeight: 200,
    canvasSize,
    fitCanvasSize: canvasSize,
  });
  const color = element.color
    ? `&color=${encodeURIComponent(element.color)}`
    : "";
  const src = `https://api.iconify.design/${element.iconName}.svg?width=200&height=200${color}`;

  // biome-ignore lint/performance/noImgElement: 贴纸就是图标 SVG
  return (
    <img
      src={src}
      alt={element.iconName}
      draggable={false}
      style={{ ...layerStyle({ element, box, scale }), objectFit: "fill" }}
    />
  );
}

function BlurBackdropLayer({
  media,
  canvasSize,
  blurIntensity,
  scale,
}: {
  media: MediaAsset;
  canvasSize: TCanvasSize;
  blurIntensity: number;
  scale: number;
}) {
  const src = media.type === "image" ? media.url : media.thumbnailUrl;
  if (!src) return null;

  // biome-ignore lint/performance/noImgElement: 背景层直接用 blob URL
  return (
    <img
      src={src}
      alt=""
      aria-hidden
      draggable={false}
      style={{
        position: "absolute",
        left: 0,
        top: 0,
        width: canvasSize.width * scale,
        height: canvasSize.height * scale,
        objectFit: "cover",
        filter: `blur(${blurIntensity * scale}px)`,
        transform: "scale(1.4)",
        transformOrigin: "center",
        pointerEvents: "none",
      }}
    />
  );
}

function TextLayer({
  texts,
  canvasSize,
  scale,
}: {
  texts: TextElement[];
  canvasSize: TCanvasSize;
  scale: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const signature = useMemo(
    () =>
      JSON.stringify(
        texts.map((element) => [
          element.id,
          element.content,
          element.fontSize,
          element.fontFamily,
          element.fontWeight,
          element.fontStyle,
          element.color,
          element.backgroundColor,
          element.backgroundOpacity,
          element.backgroundPaddingX,
          element.backgroundPaddingY,
          element.backgroundBorderRadius,
          element.boxWidth,
          element.textAlign,
          element.stroke,
          element.shadow,
          element.opacity,
          element.transform,
        ]),
      ),
    [texts],
  );

  useEffect(() => {
    const target = canvasRef.current;
    if (!target) return;
    const context = target.getContext("2d");
    if (!context) return;
    context.clearRect(0, 0, target.width, target.height);
    if (texts.length === 0) return;

    // 还是用原来那套 TextNode 画（像素和 canvas 预览一致），
    // 但直接画进这一张静态画布：不建中转 canvas、不搬 1080p 位图，
    // 只在字幕内容/样式变化时重画一次。
    const renderer = {
      context,
      width: canvasSize.width,
      height: canvasSize.height,
      fps: 30,
    } as unknown as CanvasRenderer;

    for (const element of texts) {
      const node = new TextNode({
        ...element,
        canvasCenter: {
          x: canvasSize.width / 2,
          y: canvasSize.height / 2,
        },
        canvasHeight: canvasSize.height,
        canvasWidth: canvasSize.width,
        textBaseline: isBottomAlignedSubtitleText({ element })
          ? "bottom"
          : "middle",
      });
      void node.render({ renderer, time: element.startTime });
    }

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, canvasSize.width, canvasSize.height]);

  // 没有字幕就别挂这张画布，少一个合成层
  if (texts.length === 0) return null;

  return (
    <canvas
      ref={canvasRef}
      width={canvasSize.width}
      height={canvasSize.height}
      style={{
        position: "absolute",
        left: 0,
        top: 0,
        width: canvasSize.width * scale,
        height: canvasSize.height * scale,
        pointerEvents: "none",
      }}
    />
  );
}

export function PreviewDomLayers({
  canvasSize,
  displaySize,
}: {
  canvasSize: TCanvasSize;
  displaySize: { width: number; height: number };
}) {
  const editor = useEditor();
  const tracks = editor.timeline.getTracks();
  const mediaAssets = editor.media.getAssets();
  const activeProject = editor.project.getActive();
  const background: TBackground = activeProject.settings.background;
  const fitCanvasSize = activeProject.settings.originalCanvasSize ?? canvasSize;

  const mediaMap = useMemo(
    () => new Map(mediaAssets.map((asset) => [asset.id, asset])),
    [mediaAssets],
  );

  const scale =
    canvasSize.width > 0 ? displaySize.width / canvasSize.width : 1;
  const [time, setTime] = useState(() => editor.playback.getCurrentTime());
  const signatureRef = useRef("");
  // 正在播的视频元素：播放时钟以它为准（app 的时钟是 rAF 推的，
  // 浏览器一限流 rAF 就会停住，而 <video> 自己还在播 → 字幕会卡住不动）
  const playingVideos = useRef(
    new Map<string, { element: VideoElement; video: HTMLVideoElement }>(),
  );

  const registerVideo = useCallback(
    ({
      id,
      element,
      video,
    }: {
      id: string;
      element: VideoElement;
      video: HTMLVideoElement | null;
    }) => {
      if (!video) {
        playingVideos.current.delete(id);
        return;
      }
      playingVideos.current.set(id, { element, video });
    },
    [],
  );

  /** 视频自己的播放位置换算回时间线时间（变速片段要除速率）。 */
  const timelineTimeFromVideos = (): number | null => {
    for (const { element, video } of playingVideos.current.values()) {
      if (video.paused || !Number.isFinite(video.currentTime)) continue;
      if (element.reversed) continue;
      const rate = element.playbackRate ?? 1;
      const mapped =
        element.startTime + (video.currentTime - element.trimStart) / rate;
      if (
        mapped >= element.startTime - 0.05 &&
        mapped <= element.startTime + element.duration + 0.05
      ) {
        return mapped;
      }
    }
    return null;
  };

  /** 让 <video> 的播放/暂停、暂停时的画面位置跟 app 状态一致。
   *  放在定时器/事件里跑，而不是只在 rAF 里跑：rAF 被限流时按播放也能起来。 */
  const syncVideos = () => {
    const playing = editor.playback.getIsPlaying();
    const appTime = editor.playback.getCurrentTime();
    for (const { element, video } of playingVideos.current.values()) {
      const shouldPlay = playing && !element.reversed;
      if (shouldPlay) {
        if (video.paused && !video.ended) void video.play().catch(() => {});
        continue;
      }
      if (!video.paused) video.pause();
      const target = getVisualSourceTime({
        timelineTime: appTime,
        startTime: element.startTime,
        duration: element.duration,
        trimStart: element.trimStart,
        playbackRate: element.playbackRate ?? 1,
        reversed: element.reversed ?? false,
      });
      if (
        Number.isFinite(target) &&
        Math.abs(video.currentTime - target) > 0.05
      ) {
        video.currentTime = Math.max(target, 0);
      }
    }
  };

  const currentDisplayTime = (): number => {
    if (editor.playback.getIsPlaying()) {
      const fromVideo = timelineTimeFromVideos();
      if (fromVideo !== null) return fromVideo;
    }
    return editor.playback.getCurrentTime();
  };

  // 只有「可见元素集合变化」或「暂停时时间变化（拖播放头）」才重渲染。
  // 用 150ms 定时器 + 各种事件驱动，而不是 rAF：rAF 被浏览器限流时
  // 视频还在播，字幕不能跟着卡死。
  useEffect(() => {
    const sync = () => {
      syncVideos();
      const now = currentDisplayTime();
      const playing = editor.playback.getIsPlaying();
      const active = collectActive({ tracks, time: now });
      const ids = [
        ...active.visuals.map((element) => element.id),
        ...active.stickers.map((element) => element.id),
        ...active.texts.map((element) => element.id),
      ].join(",");
      const signature = playing ? `p|${ids}` : `s|${ids}|${Math.round(now * 30)}`;
      if (signature !== signatureRef.current) {
        signatureRef.current = signature;
        setTime(now);
      }
    };

    sync();
    const timer = window.setInterval(sync, 150);
    window.addEventListener("playback-update", sync);
    window.addEventListener("playback-seek", sync);
    document.addEventListener("pointerdown", sync);
    document.addEventListener("pointerup", sync);
    document.addEventListener("pointermove", sync);
    document.addEventListener("keydown", sync);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("playback-update", sync);
      window.removeEventListener("playback-seek", sync);
      document.removeEventListener("pointerdown", sync);
      document.removeEventListener("pointerup", sync);
      document.removeEventListener("pointermove", sync);
      document.removeEventListener("keydown", sync);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editor, tracks]);

  const active = collectActive({ tracks, time });
  const blurIntensity =
    background.type === "blur"
      ? background.blurIntensity
      : DEFAULT_BLUR_INTENSITY;

  return (
    <div
      className="absolute inset-0 overflow-hidden"
      style={{
        background: background.type === "color" ? background.color : "black",
      }}
    >
      <div
        style={{
          position: "absolute",
          left: 0,
          top: 0,
          width: displaySize.width,
          height: displaySize.height,
        }}
      >
        {background.type === "blur"
          ? active.visuals.map((element) => {
              const media = mediaMap.get(element.mediaId);
              if (!media) return null;
              return (
                <BlurBackdropLayer
                  key={`blur-${element.id}`}
                  media={media}
                  canvasSize={canvasSize}
                  blurIntensity={blurIntensity}
                  scale={scale}
                />
              );
            })
          : null}

        {active.visuals.map((element) => {
          const media = mediaMap.get(element.mediaId);
          if (!media) return null;
          if (element.type === "video") {
            return (
              <VideoLayer
                key={element.id}
                element={element}
                media={media}
                canvasSize={canvasSize}
                fitCanvasSize={fitCanvasSize}
                scale={scale}
                registerVideo={registerVideo}
              />
            );
          }
          return (
            <ImageLayer
              key={element.id}
              element={element}
              media={media}
              canvasSize={canvasSize}
              fitCanvasSize={fitCanvasSize}
              scale={scale}
            />
          );
        })}

        {active.stickers.map((element) => (
          <StickerLayer
            key={element.id}
            element={element}
            canvasSize={canvasSize}
            scale={scale}
          />
        ))}

        <TextLayer texts={active.texts} canvasSize={canvasSize} scale={scale} />
      </div>
    </div>
  );
}
"""
)

PREVIEW_DOM_LAYERS.write_text(PREVIEW_DOM_LAYERS_SOURCE + "\n", encoding="utf-8")
print(f"  wrote {PREVIEW_DOM_LAYERS.relative_to(ROOT)} (DOM 预览层)")


# 预览面板：把每帧 canvas 合成换成 DOM 层
PREVIEW_PANEL_FILE = (
    SRC / "components" / "editor" / "panels" / "preview" / "index.tsx"
)
_pp = PREVIEW_PANEL_FILE.read_text(encoding="utf-8")

if "PreviewDomLayers" not in _pp:
    _pp = _pp.replace(
        'import { useCallback, useEffect, useMemo, useRef } from "react";',
        'import { useEffect, useMemo, useRef } from "react";',
        1,
    )
    _pp = _pp.replace(
        'import { useRafLoop } from "@/hooks/use-raf-loop";' + chr(10),
        "",
        1,
    )
    _pp = _pp.replace(
        'import type { RootNode } from "@/services/renderer/nodes/root-node";' + chr(10),
        "",
        1,
    )
    _pp = _pp.replace(
        'import { formatTimeCode, getLastFrameTime } from "@/lib/time";',
        'import { formatTimeCode } from "@/lib/time";' + chr(10)
        + 'import { PreviewDomLayers } from "./preview-dom-layers";',
        1,
    )

    PREVIEW_CANVAS_SOURCE = tabsify(
        """
function PreviewCanvas() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const { width: nativeWidth, height: nativeHeight } = usePreviewSize();
  const containerSize = useContainerSize({ containerRef });
  const editor = useEditor();
  const activeProject = editor.project.getActive();

  const displaySize = useMemo(() => {
    if (
      !nativeWidth ||
      !nativeHeight ||
      containerSize.width === 0 ||
      containerSize.height === 0
    ) {
      return { width: nativeWidth ?? 0, height: nativeHeight ?? 0 };
    }

    const paddingBuffer = 4;
    const availableWidth = containerSize.width - paddingBuffer;
    const availableHeight = containerSize.height - paddingBuffer;

    const aspectRatio = nativeWidth / nativeHeight;
    const containerAspect = availableWidth / availableHeight;

    const displayWidth =
      containerAspect > aspectRatio
        ? availableHeight * aspectRatio
        : availableWidth;
    const displayHeight =
      containerAspect > aspectRatio
        ? availableHeight
        : availableWidth / aspectRatio;

    return { width: displayWidth, height: displayHeight };
  }, [nativeWidth, nativeHeight, containerSize.width, containerSize.height]);

  return (
    <div
      ref={containerRef}
      className="relative flex h-full w-full items-center justify-center"
    >
      <div
        className="relative overflow-hidden border"
        style={{
          width: displaySize.width,
          height: displaySize.height,
          background:
            activeProject.settings.background.type === "blur"
              ? "transparent"
              : activeProject?.settings.background.color,
        }}
      >
        {/* 原生视频 + DOM 字幕/贴纸：浏览器自己播，不再每帧重合成 */}
        <PreviewDomLayers
          canvasSize={{ width: nativeWidth, height: nativeHeight }}
          displaySize={displaySize}
        />
        {/* 只给交互层量尺寸用，不画东西 */}
        <canvas
          ref={canvasRef}
          width={nativeWidth}
          height={nativeHeight}
          className="pointer-events-none absolute inset-0 invisible"
          style={{ width: displaySize.width, height: displaySize.height }}
        />
        <PreviewInteractionOverlay
          canvasRef={canvasRef}
          displaySize={displaySize}
        />
      </div>
    </div>
  );
}
"""
    )

    _marker = "function PreviewCanvas() {"
    _index = _pp.find(_marker)
    if _index == -1:
        raise SystemExit(f"PreviewCanvas 不在 {PREVIEW_PANEL_FILE}")
    _pp = _pp[:_index] + PREVIEW_CANVAS_SOURCE + chr(10)
    PREVIEW_PANEL_FILE.write_text(_pp, encoding="utf-8")
    print(f"  patched {PREVIEW_PANEL_FILE.relative_to(ROOT)} (预览改 DOM 层)")


# ------------------------------------------- 片段配色 / 相邻缝隙 / 裁剪伸缩
def T(levels: int) -> str:
    return TAB * levels


def patch_text(path: Path, marker: str, edits) -> None:
    """按「精确文本 + 预期次数」打补丁；锚点不对就报错，不静默失败。"""
    text = path.read_text(encoding="utf-8")
    if marker in text:
        print(f"  keep    {path.relative_to(ROOT)}")
        return
    for old, new, expect in edits:
        count = text.count(old)
        if count != expect:
            raise SystemExit(
                f"{path.relative_to(ROOT)}: anchor {old[:70]!r} expected {expect}, got {count}"
            )
        text = text.replace(old, new)
    if marker not in text:
        raise SystemExit(f"{path.relative_to(ROOT)}: marker not applied: {marker}")
    path.write_text(text, encoding="utf-8")
    print(f"  patched {path.relative_to(ROOT)}")


# 1) 配色：视频片段青色、音频绿色、字幕橙色
TRACK_CONSTANTS = SRC / "constants" / "timeline-constants.tsx"
patch_text(
    TRACK_CONSTANTS,
    "bg-[#12A5A5]",
    [
        ('background: "transparent",', 'background: "bg-[#12A5A5]",', 1),
        ('background: "bg-[#915DBE]",', 'background: "bg-[#2E9E5B]",', 1),
        ('background: "bg-[#5DBAA0]",', 'background: "bg-[#E08A38]",', 1),
    ],
)


# 2) 同轨相邻片段之间留一条缝（视觉缝，不动时间）
TIMELINE_ELEMENT_ADJACENT = (
    SRC / "components" / "editor" / "panels" / "timeline" / "timeline-element.tsx"
)
_adjacent_block = (
    T(1) + "// 同轨紧邻的下一段：两段之间画一条缝，边界一眼能看出来" + NL
    + T(1) + "// 用显示中的时间算，拖动/缩放时缝隙也不会错位" + NL
    + T(1) + "const adjacentNextElement = track.elements.find(" + NL
    + T(2) + "(candidate) =>" + NL
    + T(3) + "candidate.id !== element.id &&" + NL
    + T(3) + "Math.abs(" + NL
    + T(4) + "candidate.startTime - (displayedStartTime + displayedDuration)," + NL
    + T(3) + ") < 0.08," + NL
    + T(1) + ");"
)
_border_old = (
    T(6) + "left: `${elementLeft}px`," + NL
    + T(6) + "width: `${elementWidth}px`,"
)
_border_new = (
    T(6) + "left: `${elementLeft}px`," + NL
    + T(6) + "width: `${elementWidth}px`," + NL
    + T(6) + "// 与下一段之间留 2px 缝（border 走 border-box，不吃时间）" + NL
    + T(6) + "borderRight: adjacentNextElement" + NL
    + T(7) + '? "2px solid rgba(0, 0, 0, 0.85)"' + NL
    + T(7) + ": undefined,"
)
patch_text(
    TIMELINE_ELEMENT_ADJACENT,
    "adjacentNextElement",
    [
        (
            T(1) + "const isMuted = canElementHaveAudio(element) && element.muted === true;",
            T(1) + "const isMuted = canElementHaveAudio(element) && element.muted === true;" + NL + _adjacent_block,
            1,
        ),
        (_border_old, _border_new, 1),
    ],
)


# 3) 裁剪伸缩：上限用「素材真实长度」，且左扩不会把内容错位
RESIZE_HOOK = SRC / "hooks" / "timeline" / "element" / "use-element-resize.ts"

_source_media_block = (
    T(1) + "// 片段能延伸到的上限是「素材本身有多长」，不是「这段保留了多少」" + NL
    + T(1) + "const sourceMediaDuration = useMemo(() => {" + NL
    + T(2) + 'if (element.type !== "video" && element.type !== "audio") {' + NL
    + T(3) + "return Number.POSITIVE_INFINITY;" + NL
    + T(2) + "}" + NL
    + T(2) + 'if (!("mediaId" in element)) return Number.POSITIVE_INFINITY;' + NL
    + T(2) + "const mediaId = (element as { mediaId: string }).mediaId;" + NL
    + T(2) + "const asset = editor.media.getAssets().find((item) => item.id === mediaId);" + NL
    + T(2) + "const duration = asset?.duration;" + NL
    + T(2) + 'return typeof duration === "number" && duration > 0' + NL
    + T(3) + "? duration" + NL
    + T(3) + ": Number.POSITIVE_INFINITY;" + NL
    + T(1) + "}, [editor, element]);"
)


def _source_duration_new(indent: int) -> str:
    return (
        T(indent) + "const keptSourceDuration =" + NL
        + T(indent + 1) + "resizing.initialTrimStart +" + NL
        + T(indent + 1) + "resizing.initialDuration * rate +" + NL
        + T(indent + 1) + "resizing.initialTrimEnd;" + NL
        + T(indent) + "// 以素材真实长度为准：每段都能扩到整条素材" + NL
        + T(indent) + "const sourceDuration = Number.isFinite(sourceMediaDuration)" + NL
        + T(indent + 1) + "? Math.max(sourceMediaDuration, keptSourceDuration)" + NL
        + T(indent + 1) + ": keptSourceDuration;"
    )


_source_duration_old = (
    T(3) + "const sourceDuration =" + NL
    + T(4) + "resizing.initialTrimStart +" + NL
    + T(4) + "resizing.initialDuration * rate +" + NL
    + T(4) + "resizing.initialTrimEnd;"
)

_left_old = (
    T(4) + "const sourceTrimDelta = newTrimStart - resizing.initialTrimStart;" + NL
    + T(4) + "const timelineDelta = sourceTrimDelta / rate;"
)
_left_new = (
    T(4) + "const rawTrimDelta = newTrimStart - resizing.initialTrimStart;" + NL
    + T(4) + "// 时间线不能出现负起点：左扩到 0 就停，别只减 trimStart（否则画面整体错位）" + NL
    + T(4) + "const timelineDelta = Math.max(rawTrimDelta / rate, -resizing.initialStartTime);" + NL
    + T(4) + "const appliedTrimStart = resizing.initialTrimStart + timelineDelta * rate;"
)

_right_old = (
    T(4) + "} else {" + NL
    + T(5) + "const newDuration = snapTimeToFrame({" + NL
    + T(6) + "time: resizing.initialDuration + resizing.initialTrimEnd / rate," + NL
    + T(6) + "fps: projectFps," + NL
    + T(5) + "});"
)
_right_new = (
    T(4) + "} else {" + NL
    + T(5) + "// 右边缘拖出片段末尾：一直扩到素材结尾" + NL
    + T(5) + "const baseDuration =" + NL
    + T(6) + "resizing.initialDuration + resizing.initialTrimEnd / rate;" + NL
    + T(5) + "const maxDuration =" + NL
    + T(6) + "(sourceDuration - resizing.initialTrimStart) / rate;" + NL
    + T(5) + "const newDuration = snapTimeToFrame({" + NL
    + T(6) + "time: Math.max(" + NL
    + T(7) + "baseDuration," + NL
    + T(7) + "Math.min(baseDuration + Math.abs(newTrimEnd) / rate, maxDuration)," + NL
    + T(6) + ")," + NL
    + T(6) + "fps: projectFps," + NL
    + T(5) + "});"
)

patch_text(
    RESIZE_HOOK,
    "sourceMediaDuration",
    [
        (
            'import { useState, useEffect, useRef, useCallback } from "react";',
            'import { useState, useEffect, useRef, useCallback, useMemo } from "react";',
            1,
        ),
        (
            T(1) + "const [resizing, setResizing] = useState<ResizeState | null>(null);",
            _source_media_block
            + NL
            + T(1)
            + "const [resizing, setResizing] = useState<ResizeState | null>(null);",
            1,
        ),
        (_source_duration_old, _source_duration_new(3), 2),
        (_left_old, _left_new, 1),
        ("setCurrentTrimStart(newTrimStart);", "setCurrentTrimStart(appliedTrimStart);", 1),
        (
            "currentTrimStartRef.current = newTrimStart;",
            "currentTrimStartRef.current = appliedTrimStart;",
            1,
        ),
        (_right_old, _right_new, 1),
    ],
)


# ------------------------------- 音频/字幕轨的体积碰撞（视频轨允许自由拉伸）
COLLISION_LIB = SRC / "lib" / "timeline" / "collision.ts"
COLLISION_LIB.parent.mkdir(parents=True, exist_ok=True)
COLLISION_LIB.write_text(
    tabsify(
        """
import type { TimelineTrack } from "@/types/timeline";

const EPSILON = 1 / 1000;

/**
 * 会不会互相压：视频主轨是「自由画布」（可以随意重叠/拉伸），
 * 音频、字幕、贴纸这些顺序排列，必须挡住彼此。
 */
export function trackHasCollision({ track }: { track: TimelineTrack }): boolean {
  return track.type !== "video";
}

export type NeighborBounds = {
  /** 能往左推到的最早时间（左侧邻居的结束点 / 0） */
  minStartTime: number;
  /** 能往右拉到的最晚时间（右侧邻居的开始点 / +∞） */
  maxEndTime: number;
};

/** 同轨左右邻居给的边界；判定用元素「当前」位置，拖动跨邻居也不会乱。 */
export function getNeighborBounds({
  track,
  elementId,
  startTime,
  duration,
}: {
  track: TimelineTrack;
  elementId: string;
  startTime: number;
  duration: number;
}): NeighborBounds {
  if (!trackHasCollision({ track })) {
    return { minStartTime: 0, maxEndTime: Number.POSITIVE_INFINITY };
  }

  const currentEnd = startTime + duration;
  let minStartTime = 0;
  let maxEndTime = Number.POSITIVE_INFINITY;

  for (const other of track.elements) {
    if (other.id === elementId) continue;
    const otherEnd = other.startTime + other.duration;
    if (otherEnd <= startTime + EPSILON) {
      minStartTime = Math.max(minStartTime, otherEnd);
      continue;
    }
    if (other.startTime >= currentEnd - EPSILON) {
      maxEndTime = Math.min(maxEndTime, other.startTime);
    }
  }

  return { minStartTime, maxEndTime };
}

/** 把「想拖到哪」压到同轨不重叠的位置；视频轨原样返回。 */
export function clampStartTimeNoOverlap({
  tracks,
  trackId,
  elementId,
  requestedStartTime,
  duration,
}: {
  tracks: TimelineTrack[];
  trackId: string;
  elementId: string;
  requestedStartTime: number;
  duration: number;
}): number {
  const track = tracks.find((item) => item.id === trackId);
  if (!track || !trackHasCollision({ track })) {
    return Math.max(0, requestedStartTime);
  }

  const element = track.elements.find((item) => item.id === elementId);
  const bounds = getNeighborBounds({
    track,
    elementId,
    startTime: element?.startTime ?? requestedStartTime,
    duration: element?.duration ?? duration,
  });
  // 左右两侧都放不下（自身比空隙还长）时贴更近的一侧，别硬塞进邻居里
  if (bounds.maxEndTime - duration < bounds.minStartTime) {
    const toLeft = Math.abs(requestedStartTime - bounds.minStartTime);
    const toRight = Math.abs(
      bounds.maxEndTime - duration - requestedStartTime,
    );
    return toLeft <= toRight
      ? bounds.minStartTime
      : Math.max(0, bounds.maxEndTime - duration);
  }

  return Math.min(
    Math.max(requestedStartTime, bounds.minStartTime),
    bounds.maxEndTime - duration,
  );
}
"""
    )
    + chr(10),
    encoding="utf-8",
)
print(f"  wrote {COLLISION_LIB.relative_to(ROOT)} (轨道碰撞)")


# 拖动片段：音频/字幕不许压到同轨别的片段
ELEMENT_INTERACTION = SRC / "hooks" / "timeline" / "element" / "use-element-interaction.ts"
patch_text(
    ELEMENT_INTERACTION,
    "clampStartTimeNoOverlap",
    [
        (
            'import { useTimelineSnapping } from "@/hooks/timeline/use-timeline-snapping";',
            'import { useTimelineSnapping } from "@/hooks/timeline/use-timeline-snapping";' + NL
            + 'import { clampStartTimeNoOverlap } from "@/lib/timeline/collision";',
            1,
        ),
        (
            T(3) + "const frameSnappedTime = snapTimeToFrame({ time: adjustedTime, fps });",
            T(3) + "const frameSnappedTime = snapTimeToFrame({ time: adjustedTime, fps });" + NL
            + T(3) + "// 音频/字幕轨有体积碰撞：拖到邻居身上会被挡住" + NL
            + T(3) + "const movingTrack = tracks.find(({ id }) => id === dragState.trackId);" + NL
            + T(3) + "const movingElementForClamp = movingTrack?.elements.find(" + NL
            + T(4) + "({ id }) => id === dragState.elementId," + NL
            + T(3) + ");" + NL
            + T(3) + "const collisionClampedTime =" + NL
            + T(4) + "dragState.elementId && dragState.trackId && movingElementForClamp" + NL
            + T(5) + "? clampStartTimeNoOverlap({" + NL
            + T(6) + "tracks," + NL
            + T(6) + "trackId: dragState.trackId," + NL
            + T(6) + "elementId: dragState.elementId," + NL
            + T(6) + "requestedStartTime: frameSnappedTime," + NL
            + T(6) + "duration: movingElementForClamp.duration," + NL
            + T(5) + "})" + NL
            + T(5) + ": frameSnappedTime;",
            1,
        ),
        (
            T(3) + "const { snappedTime, snapPoint } = getDragSnapResult({" + NL
            + T(4) + "frameSnappedTime,",
            T(3) + "const { snappedTime: rawSnappedTime, snapPoint } = getDragSnapResult({" + NL
            + T(4) + "frameSnappedTime: collisionClampedTime,",
            1,
        ),
        (
            T(3) + "setDragState((previousDragState) => ({" + NL
            + T(4) + "...previousDragState," + NL
            + T(4) + "currentTime: snappedTime,",
            T(3) + "const snappedTime = rawSnappedTime;" + NL
            + T(3) + "setDragState((previousDragState) => ({" + NL
            + T(4) + "...previousDragState," + NL
            + T(4) + "currentTime: snappedTime,",
            1,
        ),
        (
            T(3) + "const snappedTime = dragState.currentTime;",
            T(3) + "const rawDropTime = dragState.currentTime;" + NL
            + T(3) + "const dropElement = tracks" + NL
            + T(4) + ".find(({ id }) => id === dragState.trackId)" + NL
            + T(4) + "?.elements.find(({ id }) => id === dragState.elementId);" + NL
            + T(3) + "// 落点再夹一次，保证提交值也合法" + NL
            + T(3) + "const snappedTime =" + NL
            + T(4) + "dragState.elementId && dragState.trackId && dropElement" + NL
            + T(5) + "? clampStartTimeNoOverlap({" + NL
            + T(6) + "tracks," + NL
            + T(6) + "trackId: dragState.trackId," + NL
            + T(6) + "elementId: dragState.elementId," + NL
            + T(6) + "requestedStartTime: rawDropTime," + NL
            + T(6) + "duration: dropElement.duration," + NL
            + T(5) + "})" + NL
            + T(5) + ": rawDropTime;",
            1,
        ),
    ],
)


# 边缘拉伸：音频/字幕不许吃进邻居
patch_text(
    RESIZE_HOOK,
    "neighborBounds",
    [
        (
            'import { useTimelineStore } from "@/stores/timeline-store";',
            'import { useTimelineStore } from "@/stores/timeline-store";' + NL
            + 'import { getNeighborBounds } from "@/lib/timeline/collision";',
            1,
        ),
        (
            T(1) + "const [resizing, setResizing] = useState<ResizeState | null>(null);",
            T(1) + "// 同轨左右邻居的边界（音频/字幕有体积碰撞，视频轨不限制）" + NL
            + T(1) + "const neighborBounds = useMemo(" + NL
            + T(2) + "() =>" + NL
            + T(3) + "getNeighborBounds({" + NL
            + T(4) + "track," + NL
            + T(4) + "elementId: element.id," + NL
            + T(4) + "startTime: element.startTime," + NL
            + T(4) + "duration: element.duration," + NL
            + T(3) + "})," + NL
            + T(2) + "[track, element.id, element.startTime, element.duration]," + NL
            + T(1) + ");" + NL
            + T(1) + "const [resizing, setResizing] = useState<ResizeState | null>(null);",
            1,
        ),
        (
            T(4) + "const timelineDelta = Math.max(rawTrimDelta / rate, -resizing.initialStartTime);",
            T(4) + "// 左扩不能越过左侧邻居，也不能越过时间轴 0 点" + NL
            + T(4) + "const minLeftShift = Math.max(" + NL
            + T(5) + "-resizing.initialStartTime," + NL
            + T(5) + "neighborBounds.minStartTime - resizing.initialStartTime," + NL
            + T(4) + ");" + NL
            + T(4) + "const timelineDelta = Math.max(rawTrimDelta / rate, minLeftShift);",
            1,
        ),
        (
            T(5) + "const maxDuration =" + NL
            + T(6) + "(sourceDuration - resizing.initialTrimStart) / rate;",
            T(5) + "const maxDuration = Math.min(" + NL
            + T(6) + "(sourceDuration - resizing.initialTrimStart) / rate," + NL
            + T(6) + "neighborBounds.maxEndTime - resizing.initialStartTime," + NL
            + T(5) + ");",
            1,
        ),
        (
            T(4) + "const clampedTrimEnd = Math.min(maxTrimEnd, Math.max(0, newTrimEnd));",
            T(4) + "// 音频/字幕：右端也不许吃进右侧邻居（视频轨 maxEndTime 是 +∞，不受影响）" + NL
            + T(4) + "const maxDurationByNeighbor =" + NL
            + T(5) + "neighborBounds.maxEndTime - resizing.initialStartTime;" + NL
            + T(4) + "const minTrimEndByNeighbor = Number.isFinite(maxDurationByNeighbor)" + NL
            + T(5) + "? resizing.initialTrimEnd -" + NL
            + T(6) + "(maxDurationByNeighbor - resizing.initialDuration) * rate" + NL
            + T(5) + ": Number.NEGATIVE_INFINITY;" + NL
            + T(4) + "const clampedTrimEnd = Math.min(" + NL
            + T(5) + "maxTrimEnd," + NL
            + T(5) + "Math.max(Math.max(0, minTrimEndByNeighbor), newTrimEnd)," + NL
            + T(4) + ");",
            1,
        ),
    ],
)


# 选中片段时，它两侧的「添加转场」加号让位，方便直接拖动
patch_text(
    SRC / "components" / "editor" / "panels" / "timeline" / "timeline-track.tsx",
    "suppressedElementIds",
    [
        (
            T(1) + "const { isElementSelected, clearElementSelection } = useElementSelection();",
            T(1) + "const { isElementSelected, clearElementSelection, selectedElements } =" + NL
            + T(2) + "useElementSelection();" + NL
            + NL
            + T(1) + "// 选中的片段：它两侧的「添加转场」加号会挡住拖动，先收起来" + NL
            + T(1) + "const suppressedJunctionIds = selectedElements" + NL
            + T(2) + ".filter((entry) => entry.trackId === track.id)" + NL
            + T(2) + ".map((entry) => entry.elementId);",
            1,
        ),
        (
            T(7) + "<TrackTransitionOverlays" + NL
            + T(8) + "track={track as VideoTrack}" + NL
            + T(8) + "zoomLevel={zoomLevel}" + NL
            + T(7) + "/>",
            T(7) + "<TrackTransitionOverlays" + NL
            + T(8) + "track={track as VideoTrack}" + NL
            + T(8) + "zoomLevel={zoomLevel}" + NL
            + T(8) + "suppressedElementIds={suppressedJunctionIds}" + NL
            + T(7) + "/>",
            1,
        ),
    ],
)

patch_text(
    SRC / "components" / "editor" / "panels" / "timeline" / "timeline-transition-overlay.tsx",
    "suppressedElementIds",
    [
        (
            T(1) + "track," + NL + T(1) + "zoomLevel," + NL
            + "}: {" + NL
            + T(1) + "track: VideoTrack;" + NL
            + T(1) + "zoomLevel: number;" + NL
            + "}) {",
            T(1) + "track," + NL + T(1) + "zoomLevel," + NL
            + T(1) + "suppressedElementIds = []," + NL
            + "}: {" + NL
            + T(1) + "track: VideoTrack;" + NL
            + T(1) + "zoomLevel: number;" + NL
            + T(1) + "/** 这些元素被选中：与它们相邻的接缝先不显示加号，免得挡住拖动 */" + NL
            + T(1) + "suppressedElementIds?: string[];" + NL
            + "}) {",
            1,
        ),
        (
            T(3) + "{pairs.map((pair) => {" + NL
            + T(4) + "const transition = getTransitionForPair({",
            T(3) + "{pairs.map((pair) => {" + NL
            + T(4) + "const suppressed =" + NL
            + T(5) + "suppressedElementIds.includes(pair.from.id) ||" + NL
            + T(5) + "suppressedElementIds.includes(pair.to.id);" + NL
            + T(4) + "if (suppressed) return null;" + NL
            + NL
            + T(4) + "const transition = getTransitionForPair({",
            1,
        ),
    ],
)


# ------------------------------- 字幕/文字的拉伸也要被邻居挡住（canExtend 分支）
patch_text(
    RESIZE_HOOK,
    "maxExtensionByNeighbor",
    [
        (
            T(5) + "const maxExtension = resizing.initialStartTime;",
            T(5) + "// 字幕/文字能无限延长（maxExtensionByNeighbor），但同样不许吃进左邻居" + NL
            + T(5) + "const maxExtension = Math.min(" + NL
            + T(6) + "resizing.initialStartTime," + NL
            + T(6) + "resizing.initialStartTime - neighborBounds.minStartTime," + NL
            + T(5) + ");",
            1,
        ),
        (
            T(6) + "time: baseDuration + extensionNeeded,",
            T(6) + "// 右端也不许吃进右邻居（视频轨 maxEndTime 是 +∞）" + NL
            + T(6) + "time: Math.min(" + NL
            + T(7) + "baseDuration + extensionNeeded," + NL
            + T(7) + "neighborBounds.maxEndTime - resizing.initialStartTime," + NL
            + T(6) + "),",
            1,
        ),
    ],
)


# ------------------------------- 播放头吸附到裁剪点（小阈值 + 迟滞）
PLAYHEAD_SNAPPING = SRC / "lib" / "timeline" / "playhead-snapping.ts"
PLAYHEAD_SNAPPING.write_text(
    tabsify(
        """
import type { TimelineTrack } from "@/types/timeline";

export type PlayheadSnapResult = {
  time: number;
  snapTime: number | null;
};

/**
 * 拖动播放头时吸附到片段裁剪点。
 *
 * 进吸附的阈值小（stickPx），一旦吸附住要挪出更远（releasePx）才脱开，
 * 这样能明显感觉到「被吸住了」，又不会在稍远处乱跳。
 */
export function snapPlayheadTime({
  tracks,
  rawTime,
  pixelsPerSecond,
  activeSnapTime = null,
  stickPx = 8,
  releasePx = 14,
}: {
  tracks: TimelineTrack[];
  rawTime: number;
  pixelsPerSecond: number;
  activeSnapTime?: number | null;
  stickPx?: number;
  releasePx?: number;
}): PlayheadSnapResult {
  if (pixelsPerSecond <= 0) {
    return { time: rawTime, snapTime: null };
  }

  const stickSeconds = stickPx / pixelsPerSecond;
  const releaseSeconds = releasePx / pixelsPerSecond;

  // 已经吸附住：只有挪出 releaseSeconds 才脱开
  if (
    activeSnapTime !== null &&
    Math.abs(rawTime - activeSnapTime) <= releaseSeconds
  ) {
    return { time: activeSnapTime, snapTime: activeSnapTime };
  }

  let best: number | null = null;
  let bestDistance = stickSeconds;

  for (const track of tracks) {
    if ("hidden" in track && track.hidden) continue;
    for (const element of track.elements) {
      const candidates = [
        element.startTime,
        element.startTime + element.duration,
      ];
      for (const candidate of candidates) {
        const distance = Math.abs(candidate - rawTime);
        if (distance <= bestDistance) {
          best = candidate;
          bestDistance = distance;
        }
      }
    }
  }

  if (best === null) {
    return { time: rawTime, snapTime: null };
  }
  return { time: best, snapTime: best };
}
"""
    )
    + chr(10),
    encoding="utf-8",
)
print(f"  wrote {PLAYHEAD_SNAPPING.relative_to(ROOT)} (播放头吸附)")


# 拖动/点击播放头：帧对齐之后再吸附到裁剪点
patch_text(
    SRC / "hooks" / "timeline" / "use-timeline-playhead.ts",
    "snapPlayheadTime",
    [
        (
            'import { getSnappedSeekTime } from "@/lib/time";',
            'import { getSnappedSeekTime } from "@/lib/time";' + NL
            + 'import { snapPlayheadTime } from "@/lib/timeline/playhead-snapping";',
            1,
        ),
        (
            T(1) + "const lastMouseXRef = useRef<number>(0);",
            T(1) + "const lastMouseXRef = useRef<number>(0);" + NL
            + T(1) + "// 当前吸住的那个裁剪点（迟滞用）" + NL
            + T(1) + "const snapTimeRef = useRef<number | null>(null);",
            1,
        ),
        (
            T(3) + "const time = getSnappedSeekTime({" + NL
            + T(4) + "rawTime," + NL
            + T(4) + "duration," + NL
            + T(4) + "fps: framesPerSecond," + NL
            + T(3) + "});" + NL
            + NL
            + T(3) + "setScrubTime(time);" + NL
            + T(3) + "seek({ time });",
            T(3) + "const frameTime = getSnappedSeekTime({" + NL
            + T(4) + "rawTime," + NL
            + T(4) + "duration," + NL
            + T(4) + "fps: framesPerSecond," + NL
            + T(3) + "});" + NL
            + T(3) + "const snapped = snapPlayheadTime({" + NL
            + T(4) + "tracks: editor.timeline.getTracks()," + NL
            + T(4) + "rawTime: frameTime," + NL
            + T(4) + "pixelsPerSecond: TIMELINE_CONSTANTS.PIXELS_PER_SECOND * zoomLevel," + NL
            + T(4) + "activeSnapTime: snapTimeRef.current," + NL
            + T(3) + "});" + NL
            + T(3) + "snapTimeRef.current = snapped.snapTime;" + NL
            + T(3) + "const time = snapped.time;" + NL
            + NL
            + T(3) + "setScrubTime(time);" + NL
            + T(3) + "seek({ time });",
            1,
        ),
        (
            T(3) + "editor.playback.setScrubbing({ isScrubbing: false });" + NL
            + T(3) + "if (scrubTime !== null) {",
            T(3) + "editor.playback.setScrubbing({ isScrubbing: false });" + NL
            + T(3) + "snapTimeRef.current = null;" + NL
            + T(3) + "if (scrubTime !== null) {",
            1,
        ),
    ],
)

patch_text(
    SRC / "hooks" / "timeline" / "use-timeline-seek.ts",
    "snapPlayheadTime",
    [
        (
            'import { getSnappedSeekTime } from "@/lib/time";',
            'import { getSnappedSeekTime } from "@/lib/time";' + NL
            + 'import { snapPlayheadTime } from "@/lib/timeline/playhead-snapping";',
            1,
        ),
        (
            T(1) + "const mouseTrackingRef = useRef({" + NL,
            T(1) + "// 当前吸住的那个裁剪点（迟滞用）" + NL
            + T(1) + "const snapTimeRef = useRef<number | null>(null);" + NL
            + T(1) + "const mouseTrackingRef = useRef({" + NL,
            1,
        ),
        (
            T(3) + "const time = getSnappedSeekTime({" + NL
            + T(4) + "rawTime," + NL
            + T(4) + "duration," + NL
            + T(4) + "fps: projectFps," + NL
            + T(3) + "});" + NL
            + T(3) + "seek(time);",
            T(3) + "const frameTime = getSnappedSeekTime({" + NL
            + T(4) + "rawTime," + NL
            + T(4) + "duration," + NL
            + T(4) + "fps: projectFps," + NL
            + T(3) + "});" + NL
            + T(3) + "const snapped = snapPlayheadTime({" + NL
            + T(4) + "tracks: editor.timeline.getTracks()," + NL
            + T(4) + "rawTime: frameTime," + NL
            + T(4) + "pixelsPerSecond: TIMELINE_CONSTANTS.PIXELS_PER_SECOND * zoomLevel," + NL
            + T(4) + "activeSnapTime: snapTimeRef.current," + NL
            + T(3) + "});" + NL
            + T(3) + "snapTimeRef.current = snapped.snapTime;" + NL
            + T(3) + "const time = snapped.time;" + NL
            + T(3) + "seek(time);",
            1,
        ),
    ],
)


# ---------------------------------------- 清掉未使用的 import（构建期 tsc 不检查）
patch_text(
    SRC / "components" / "editor" / "editor-header.tsx",
    T(0) + "export function EditorHeader() {" + NL + T(1) + "return (",
    [
        (
            T(0) + "export function EditorHeader() {" + NL
            + T(1) + "const { t } = useTranslation();" + NL,
            T(0) + "export function EditorHeader() {" + NL,
            1,
        ),
    ],
)

patch_text(
    SRC / "components" / "editor" / "panels" / "assets" / "views" / "captions.tsx",
    "[storedModelId]",
    [
        (
            T(1) + "const [storedModelId, setSelectedModelId] =" + NL,
            T(1) + "const [storedModelId] =" + NL,
            1,
        ),
    ],
)


# ------------------------------- 终态任务超时回收（渲染 / 人声美化的 job 表）
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _patch_plugin(path: Path, marker: str, edits) -> None:
    """插件自己的 Python 文件（不在 ROOT 工作副本里）：幂等 + 次数校验。"""
    text = path.read_text(encoding="utf-8")
    if marker in text:
        print(f"  keep    {path.name}")
        return
    for old, new, expect in edits:
        count = text.count(old)
        if count != expect:
            raise SystemExit(
                f"{path.name}: anchor {old[:70]!r} expected {expect}, got {count}"
            )
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    print(f"  patched {path.name}")


_patch_plugin(
    PLUGIN_ROOT / "yuancut_render.py",
    "_sweep_jobs",
    [
        (
            "import tempfile" + NL + "import uuid",
            "import tempfile" + NL + "import time" + NL + "import uuid",
            1,
        ),
        (
            "_JOBS: dict[str, dict] = {}",
            "_JOBS: dict[str, dict] = {}" + NL
            + NL
            + "#: 终态任务留这么久，够客户端回来取结果；再久就回收，别让内存和临时目录越堆越多" + NL
            + "_JOB_TTL_SECONDS = 30 * 60" + NL
            + NL
            + NL
            + "def _sweep_jobs() -> None:" + NL
            + "    now = time.time()" + NL
            + "    for job_id, job in list(_JOBS.items()):" + NL
            + '        if job.get("state") not in ("done", "error", "cancelled"):' + NL
            + "            continue" + NL
            + '        finished = job.get("finishedAt")' + NL
            + "        if finished is None:" + NL
            + '            job["finishedAt"] = now' + NL
            + "            continue" + NL
            + "        if now - finished < _JOB_TTL_SECONDS:" + NL
            + "            continue" + NL
            + '        work = job.get("work")' + NL
            + "        if isinstance(work, Path):" + NL
            + "            shutil.rmtree(work, ignore_errors=True)" + NL
            + "        _JOBS.pop(job_id, None)",
            1,
        ),
        (
            "async def render_start(request: web.Request) -> web.Response:" + NL
            + "    try:" + NL
            + "        timeline = await request.json()",
            "async def render_start(request: web.Request) -> web.Response:" + NL
            + "    _sweep_jobs()" + NL
            + "    try:" + NL
            + "        timeline = await request.json()",
            1,
        ),
    ],
)

_patch_plugin(
    PLUGIN_ROOT / "yuancut_voice.py",
    "_sweep_jobs",
    [
        (
            "_worker_task: asyncio.Task | None = None",
            "_worker_task: asyncio.Task | None = None" + NL
            + NL
            + "#: 终态任务留这么久，够客户端回来取结果；再久就回收" + NL
            + "_JOB_TTL_SECONDS = 30 * 60" + NL
            + NL
            + NL
            + "def _sweep_jobs() -> None:" + NL
            + "    now = time.time()" + NL
            + "    for job_id, job in list(_jobs.items()):" + NL
            + '        if job.get("status") not in ("done", "error"):' + NL
            + "            continue" + NL
            + '        finished = job.get("finishedAt")' + NL
            + "        if finished is None:" + NL
            + '            job["finishedAt"] = now' + NL
            + "            continue" + NL
            + "        if now - finished < _JOB_TTL_SECONDS:" + NL
            + "            continue" + NL
            + '        workdir = job.get("workdir")' + NL
            + "        if isinstance(workdir, Path):" + NL
            + "            shutil.rmtree(workdir, ignore_errors=True)" + NL
            + "        _jobs.pop(job_id, None)",
            1,
        ),
        (
            "async def voice_beautify(request: web.Request) -> web.Response:  # noqa: ANN001" + NL
            + "    ok, reason = availability()",
            "async def voice_beautify(request: web.Request) -> web.Response:  # noqa: ANN001" + NL
            + "    _sweep_jobs()" + NL
            + "    ok, reason = availability()",
            1,
        ),
    ],
)


# ---------------------------------- 清掉上游自带、从未被引用的 import / 参数
def strip_unused(path: Path, removals) -> None:
    """按片段做纯删除；片段已经不在了就当已清理过（幂等）。"""
    text = path.read_text(encoding="utf-8")
    removed = 0
    for chunk in removals:
        if chunk in text:
            text = text.replace(chunk, "", 1)
            removed += 1
    if removed:
        path.write_text(text, encoding="utf-8")
        print(f"  cleaned {path.relative_to(ROOT)} ({removed} 处)")
    else:
        print(f"  keep    {path.relative_to(ROOT)}")


strip_unused(
    SRC / "app" / "[locale]" / "projects" / "page.tsx",
    [
        T(1) + "BreadcrumbLink," + NL,
        T(1) + "BreadcrumbSeparator," + NL,
    ],
)

strip_unused(
    SRC / "components" / "ui" / "alert.tsx",
    ['import { AlertTriangle } from "lucide-react";' + NL],
)

strip_unused(
    SRC / "components" / "ui" / "chart.tsx",
    [
        "import {" + NL
        + T(1) + "NameType," + NL
        + T(1) + "Payload," + NL
        + T(1) + "ValueType," + NL
        + '} from "recharts/types/component/DefaultTooltipContent";' + NL
        + NL,
    ],
)

patch_text(
    SRC / "components" / "editor" / "panels" / "assets" / "views" / "settings.tsx",
    'import { memo, useCallback, useMemo } from "react";',
    [
        (
            'import { memo, useCallback, useMemo, useState } from "react";',
            'import { memo, useCallback, useMemo } from "react";',
            1,
        ),
        (T(1) + "FPS_PRESETS," + NL, "", 1),
    ],
)

patch_text(
    SRC / "components" / "ui" / "calendar.tsx",
    "IconLeft: () =>",
    [
        ("IconLeft: ({ ...props }) =>", "IconLeft: () =>", 1),
        ("IconRight: ({ ...props }) =>", "IconRight: () =>", 1),
    ],
)

patch_text(
    SRC / "core" / "managers" / "media-manager.ts",
    "constructor(_editor: EditorCore) {}",
    [
        (
            "constructor(private editor: EditorCore) {}",
            "constructor(_editor: EditorCore) {}",
            1,
        ),
    ],
)


# 轮询时也顺手回收过期任务（只 start 时清，失败任务可能一直躺到下次导出）
_patch_plugin(
    PLUGIN_ROOT / "yuancut_render.py",
    '_sweep_jobs()' + NL + '    job = _job(request.query.get("jobId", ""))',
    [
        (
            "async def render_progress(request: web.Request) -> web.Response:" + NL
            + '    job = _job(request.query.get("jobId", ""))',
            "async def render_progress(request: web.Request) -> web.Response:" + NL
            + "    _sweep_jobs()" + NL
            + '    job = _job(request.query.get("jobId", ""))',
            1,
        ),
    ],
)

_patch_plugin(
    PLUGIN_ROOT / "yuancut_voice.py",
    "_sweep_jobs()" + NL + '    job_id = request.query.get("jobId", "")',
    [
        (
            "async def voice_progress(request: web.Request) -> web.Response:  # noqa: ANN001" + NL
            + '    job_id = request.query.get("jobId", "")',
            "async def voice_progress(request: web.Request) -> web.Response:  # noqa: ANN001" + NL
            + "    _sweep_jobs()" + NL
            + '    job_id = request.query.get("jobId", "")',
            1,
        ),
        (
            '            f"[voice] 处理完成 {job[\'output\'].name}，用时 {time.time() - job[\'startedAt\']:.1f}s"' + NL
            + "        )" + NL
            + NL
            + "def _refresh_progress(job: dict) -> None:",
            '            f"[voice] 处理完成 {job[\'output\'].name}，用时 {time.time() - job[\'startedAt\']:.1f}s"' + NL
            + "        )" + NL
            + NL
            + NL
            + "def _refresh_progress(job: dict) -> None:",
            1,
        ),
    ],
)


# =====================================================================
# 框选 / 多选：键盘不再被文本选区吃掉 + 属性面板按「全部选中」生效
# ---------------------------------------------------------------------
# 1) 键盘：时间轴框选会在文档里留下一个文本选区。上游的规则是「文档里只要有
#    选中的文字就跳过所有快捷键」，于是框选完按 Delete 经常没反应（有时又
#    正常 —— 取决于上一次点击有没有清掉那个选区）。这里改成只看选区是不是
#    落在可输入区域里，并且框选结束主动清掉残留选区。
# 2) 属性面板：视频/音频/贴纸原来只对「第一个选中」生效。改成把改动分发到
#    所有选中片段：变速按每个片段自己的当前速度换算时长，人声美化按素材去重
#    后逐个处理再回填。
# =====================================================================

KEYBINDINGS = SRC / "hooks" / "use-keybindings.ts"

patch_text(
    KEYBINDINGS,
    "function isSelectionInsideEditable()",
    [
        (
            "export function useKeybindingsListener() {",
            "/**\n"
            " * 只有「选区在输入框 / 可编辑区里」才让快捷键让路。\n"
            " * 时间轴框选、拖动片段都会在文档里留下一个文本选区，而这条路径上的\n"
            " * 选区跟输入无关，不能因此把 Delete 之类的快捷键一起吞掉。\n"
            " */\n"
            "function isSelectionInsideEditable(): boolean {\n"
            "\tconst selection = window.getSelection();\n"
            "\tif (!selection || selection.isCollapsed || selection.rangeCount === 0) {\n"
            "\t\treturn false;\n"
            "\t}\n"
            "\tconst anchor = selection.anchorNode;\n"
            "\tconst element =\n"
            "\t\tanchor instanceof Element ? anchor : (anchor?.parentElement ?? null);\n"
            "\tif (!element) return false;\n"
            "\treturn Boolean(\n"
            "\t\telement.closest(\n"
            "\t\t\t'input, textarea, [contenteditable=\"\"], [contenteditable=\"true\"], [data-keybinding-free]',\n"
            "\t\t),\n"
            "\t);\n"
            "}\n"
            "\n"
            "export function useKeybindingsListener() {",
            1,
        ),
        (
            "\t\t\treturn (window.getSelection()?.toString().length ?? 0) > 0;",
            "\t\t\treturn isSelectionInsideEditable();",
            1,
        ),
    ],
)

SELECTION_BOX = SRC / "hooks" / "timeline" / "use-selection-box.ts"

patch_text(
    SELECTION_BOX,
    "blockSelectStart",
    [
        (
            "\t\t\tif (!isEnabled) return;\n"
            "\n"
            "\t\t\t// Keep the starting selection fixed while the box grows or shrinks.",
            "\t\t\tif (!isEnabled) return;\n"
            "\n"
            "\t\t\t// 框选开始时清掉文档里残留的文本选区：\n"
            "\t\t\t// 否则快捷键会被当成「用户正在选文字」而全部跳过\n"
            "\t\t\twindow.getSelection()?.removeAllRanges();\n"
            "\n"
            "\t\t\t// Keep the starting selection fixed while the box grows or shrinks.",
            1,
        ),
        (
            "\t\tconst handleMouseUp = () => {\n"
            "\t\t\tif (selectionBox?.isActive) {\n"
            "\t\t\t\tjustFinishedSelectingRef.current = true;",
            "\t\tconst handleMouseUp = () => {\n"
            "\t\t\tif (selectionBox?.isActive) {\n"
            "\t\t\t\t// 拖动本身可能顺手选中了片段名之类的文字，一并清掉\n"
            "\t\t\t\twindow.getSelection()?.removeAllRanges();\n"
            "\t\t\t\tjustFinishedSelectingRef.current = true;",
            1,
        ),
        (
            "\t\tdocument.body.style.userSelect = \"none\";\n"
            "\t\tif (container) container.style.userSelect = \"none\";\n",
            "\t\tdocument.body.style.userSelect = \"none\";\n"
            "\t\tif (container) container.style.userSelect = \"none\";\n"
            "\t\tconst blockSelectStart = (event: Event) => event.preventDefault();\n"
            "\t\tdocument.addEventListener(\"selectstart\", blockSelectStart);\n",
            1,
        ),
        (
            "\t\t\tif (container) container.style.userSelect = previousContainerUserSelect;\n"
            "\t\t};",
            "\t\t\tif (container) container.style.userSelect = previousContainerUserSelect;\n"
            "\t\t\tdocument.removeEventListener(\"selectstart\", blockSelectStart);\n"
            "\t\t};",
            1,
        ),
    ],
)


# ------------------------------------------------- 属性面板：多选集体编辑
PROPERTIES_DIR = SRC / "components" / "editor" / "panels" / "properties"
VIDEO_PROPERTIES = PROPERTIES_DIR / "video-properties.tsx"
AUDIO_PROPERTIES = PROPERTIES_DIR / "audio-properties.tsx"
STICKER_PROPERTIES = PROPERTIES_DIR / "sticker-properties.tsx"
PROPERTIES_PANEL = PROPERTIES_DIR / "index.tsx"

PROPERTIES_PANEL_TSX = r'''"use client";

import { useMemo } from "react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { AudioProperties } from "./audio-properties";
import { VideoProperties } from "./video-properties";
import { TextProperties } from "./text-properties";
import { StickerProperties } from "./sticker-properties";
import { EmptyView } from "./empty-view";
import { useEditor } from "@/hooks/use-editor";
import { useElementSelection } from "@/hooks/timeline/element/use-element-selection";
import type {
	AudioElement,
	ImageElement,
	StickerElement,
	TextElement,
	TimelineElement,
	TimelineTrack,
	VideoElement,
} from "@/types/timeline";

interface ElementWithTrack {
	element: TimelineElement;
	track: TimelineTrack;
}

function groupByType(items: ElementWithTrack[]) {
	const groups: Record<string, ElementWithTrack[]> = {};
	for (const item of items) {
		const key = item.element.type;
		if (!groups[key]) groups[key] = [];
		groups[key].push(item);
	}
	return groups;
}

function toRefs(items: ElementWithTrack[]) {
	return items.map((item) => ({ element: item.element, trackId: item.track.id }));
}

/**
 * 属性面板：每个分组都把「整组选中片段」交给对应的面板，
 * 面板内部对全部选中生效（数值显示取第一个，修改分发到所有）。
 */
export function PropertiesPanel() {
	const editor = useEditor();
	const { selectedElements } = useElementSelection();

	const elementsWithTracks = editor.timeline.getElementsWithTracks({
		elements: selectedElements,
	});

	const grouped = useMemo(
		() => groupByType(elementsWithTracks),
		[elementsWithTracks],
	);

	const total = selectedElements.length;

	return (
		<div className="panel bg-background h-full rounded-sm border overflow-hidden">
			{total > 0 ? (
				<div className="flex h-full flex-col">
					{total > 1 ? (
						<div className="text-muted-foreground border-b px-4 py-1.5 text-[11px] leading-tight">
							已选 {total} 个片段：下面的修改会一起应用
						</div>
					) : null}
					<ScrollArea className="min-h-0 flex-1">
						{grouped.text && grouped.text.length > 0 && (
							<TextProperties
								elements={
									toRefs(grouped.text) as unknown as {
										element: TextElement;
										trackId: string;
									}[]
								}
							/>
						)}
						{grouped.video && grouped.video.length > 0 && (
							<VideoProperties
								elements={
									toRefs(grouped.video) as unknown as {
										element: VideoElement | ImageElement;
										trackId: string;
									}[]
								}
							/>
						)}
						{grouped.image && grouped.image.length > 0 && (
							<VideoProperties
								elements={
									toRefs(grouped.image) as unknown as {
										element: VideoElement | ImageElement;
										trackId: string;
									}[]
								}
							/>
						)}
						{grouped.audio && grouped.audio.length > 0 && (
							<AudioProperties
								elements={
									toRefs(grouped.audio) as unknown as {
										element: AudioElement;
										trackId: string;
									}[]
								}
							/>
						)}
						{grouped.sticker && grouped.sticker.length > 0 && (
							<StickerProperties
								elements={
									toRefs(grouped.sticker) as unknown as {
										element: StickerElement;
										trackId: string;
									}[]
								}
							/>
						)}
					</ScrollArea>
				</div>
			) : (
				<EmptyView />
			)}
		</div>
	);
}
'''

write(PROPERTIES_PANEL, PROPERTIES_PANEL_TSX)


# ---------------------------------------------------- 视频/图片属性：多选
patch_text(
    VIDEO_PROPERTIES,
    'import { useEffect, useMemo, useReducer, useRef, useState } from "react";',
    [
        (
            'import { useEffect, useReducer, useRef, useState } from "react";',
            'import { useEffect, useMemo, useReducer, useRef, useState } from "react";',
            1,
        ),
    ],
)

replace_between(
    VIDEO_PROPERTIES,
    "export function VideoProperties({",
    "\tconst [beautifyBusy, setBeautifyBusy] = useState(false);",
    r'''export function VideoProperties({
	elements: elementRefs,
}: {
	elements: { element: VideoElement | ImageElement; trackId: string }[];
}) {
	// 数值显示取第一个选中片段；修改分发到全部选中片段（多选 = 集体操作）
	const element = elementRefs[0].element;
	const { t } = useTranslation();
	const editor = useEditor();
	const [, forceRender] = useReducer((x: number) => x + 1, 0);
	const videoRefs = useMemo(
		() =>
			elementRefs.filter((ref) => ref.element.type === "video") as {
				element: VideoElement;
				trackId: string;
			}[],
		[elementRefs],
	);''',
    marker="elements: { element: VideoElement | ImageElement; trackId: string }[];",
)

replace_between(
    VIDEO_PROPERTIES,
    '\tconst probeAssetId = element.type === "video" ? element.mediaId : "";',
    "\tconst beautifyStoredEnabled =",
    r'''	const probeAssetIds = videoRefs.map((ref) => ref.element.mediaId).join("|");
	useEffect(() => {
		setAssetHasAudio(null);
		// 多选时只要有一个选中素材带音轨，就允许做整组人声美化
		const ids = probeAssetIds.split("|").filter(Boolean);
		if (ids.length === 0) return;
		let cancelled = false;
		void (async () => {
			for (const id of ids) {
				const media =
					editor.media.getAssets().find((asset) => asset.id === id) ?? null;
				const pending = probeMediaHasAudio({ media });
				if (!pending) continue;
				const hasAudio = await pending;
				if (cancelled) return;
				if (hasAudio) {
					setAssetHasAudio(true);
					return;
				}
			}
			if (!cancelled) setAssetHasAudio(false);
		})();
		return () => {
			cancelled = true;
		};
	}, [editor, probeAssetIds]);

''',
    marker="// 多选时只要有一个选中素材带音轨",
)

patch_text(
    VIDEO_PROPERTIES,
    "videoRefs.every((ref) => ref.element.voiceBeautifyEnabled === true)",
    [
        (
            '\tconst beautifyStoredEnabled =\n'
            '\t\telement.type === "video" && element.voiceBeautifyEnabled === true;',
            "\t// 勾选框反映整组状态：全部勾上才算勾上\n"
            "\tconst beautifyStoredEnabled =\n"
            "\t\tvideoRefs.length > 0 &&\n"
            "\t\tvideoRefs.every((ref) => ref.element.voiceBeautifyEnabled === true);",
            1,
        ),
    ],
)

replace_between(
    VIDEO_PROPERTIES,
    "\tconst updateClip = (updates: Partial<Record<string, unknown>>) => {",
    "\tconst showBeautifyRow =",
    r'''	const updateRefs = (
		refs: { element: VideoElement | ImageElement; trackId: string }[],
		updates: Partial<Record<string, unknown>>,
		pushHistory = true,
	) => {
		if (refs.length === 0) return;
		editor.timeline.updateElements({
			updates: refs.map((ref) => ({
				trackId: ref.trackId,
				elementId: ref.element.id,
				updates,
			})),
			pushHistory,
		});
	};

	const updateBulk = (
		updates: Partial<Record<string, unknown>>,
		pushHistory = true,
	) => {
		updateRefs(elementRefs, updates, pushHistory);
	};
''',
    marker="const updateRefs = (",
)

replace_between(
    VIDEO_PROPERTIES,
    "\tconst showBeautifyRow =",
    "\tconst runVideoBeautify = async () => {",
    r'''	const showBeautifyRow =
		videoRefs.length > 0 &&
		videoRefs.some((ref) => !ref.element.muted) &&
		assetHasAudio === true;

''',
    marker="videoRefs.some((ref) => !ref.element.muted) &&",
)


replace_between(
    VIDEO_PROPERTIES,
    "\tconst runVideoBeautify = async () => {",
    "\tconst isEditingScale = useRef(false);",
    r'''	const runVideoBeautify = async () => {
		if (beautifyBusy) return;
		if (videoRefs.length === 0) return;
		// 同一个素材只处理一次：结果是独立的 wav，几个片段共用同一份即可
		const groups = new Map<
			string,
			{ element: VideoElement; trackId: string }[]
		>();
		for (const ref of videoRefs) {
			if (ref.element.muted) continue;
			const list = groups.get(ref.element.mediaId) ?? [];
			list.push(ref);
			groups.set(ref.element.mediaId, list);
		}
		if (groups.size === 0) {
			setBeautifyOverride(null);
			setBeautifyStatus("所选片段都是静音的，没有可处理的音轨");
			return;
		}
		setBeautifyBusy(true);
		setBeautifyProgress(0);
		setBeautifyStatus("准备中…");
		let index = 0;
		let processed = 0;
		let skipped = 0;
		try {
			for (const [mediaId, refs] of groups) {
				index += 1;
				const prefix =
					groups.size > 1 ? `第 ${index}/${groups.size} 个素材 · ` : "";
				const source = editor.media
					.getAssets()
					.find((asset) => asset.id === mediaId);
				if (!source) {
					skipped += refs.length;
					continue;
				}
				const hasAudio = await probeMediaHasAudio({ media: source });
				if (!hasAudio) {
					skipped += refs.length;
					continue;
				}
				const result = await applyVocalBeautify({
					editor,
					source,
					strength: beautifyStrength,
					onProgress: (state) => {
						setBeautifyStatus(`${prefix}${describeVocalBeautify(state)}`);
						setBeautifyProgress(
							((index - 1) + (state.progress ?? 0)) / groups.size,
						);
					},
				});
				updateRefs(
					refs,
					{
						voiceBeautifyEnabled: true,
						voiceBeautifyMediaId: result.mediaId,
						voiceBeautifyStrength: beautifyStrength,
					},
					true,
				);
				processed += refs.length;
			}
			setBeautifyStatus(
				skipped > 0
					? `已处理 ${processed} 个片段，${skipped} 个因没有音轨跳过`
					: `已处理 ${processed} 个片段：画面不变，声音换成处理后的音轨`,
			);
		} catch (error) {
			setBeautifyOverride(null);
			setBeautifyStatus(`失败：${(error as Error).message}`);
		} finally {
			setBeautifyBusy(false);
			setBeautifyProgress(null);
		}
	};

	const toggleVideoBeautify = (checked: boolean) => {
		if (beautifyBusy) return;
		setBeautifyOverride(checked);
		if (!checked) {
			updateRefs(videoRefs, { voiceBeautifyEnabled: false }, true);
			setBeautifyStatus("");
			return;
		}
		const targets = videoRefs.filter((ref) => !ref.element.muted);
		if (targets.length === 0) {
			setBeautifyOverride(null);
			setBeautifyStatus("所选片段都是静音的，没有可处理的音轨");
			return;
		}
		if (targets.every((ref) => Boolean(ref.element.voiceBeautifyMediaId))) {
			updateRefs(videoRefs, { voiceBeautifyEnabled: true }, true);
			setBeautifyStatus("");
			return;
		}
		void runVideoBeautify();
	};

''',
    marker="第 ${index}/${groups.size} 个素材",
)

patch_text(
    VIDEO_PROPERTIES,
    "const isVideoElement = videoRefs.length > 0;",
    [
        (
            '\tconst isVideoElement = element.type === "video";',
            "\t// 选中的里有视频片段就显示速度那一组：改动会作用到全部视频片段\n"
            "\tconst isVideoElement = videoRefs.length > 0;",
            1,
        ),
    ],
)

replace_between(
    VIDEO_PROPERTIES,
    "\tconst applySpeedChange = ({",
    "\tconst commitNumberField = ({",
    r'''	const applySpeedChange = ({
		newRate,
		pushHistory,
	}: {
		newRate: number;
		pushHistory: boolean;
	}) => {
		if (videoRefs.length === 0) return;
		// 每个片段按自己的当前速度换算时长：多选就是一次性全改
		editor.timeline.updateElements({
			updates: videoRefs.map((ref) => {
				const oldRate = ref.element.playbackRate ?? 1;
				return {
					trackId: ref.trackId,
					elementId: ref.element.id,
					updates: {
						playbackRate: newRate,
						duration: ref.element.duration * (oldRate / newRate),
					},
				};
			}),
			pushHistory,
		});
	};

	const updateTransform = ({
		updates,
		pushHistory = true,
	}: {
		updates: Partial<typeof element.transform>;
		pushHistory?: boolean;
	}) => {
		editor.timeline.updateElements({
			updates: elementRefs.map((ref) => ({
				trackId: ref.trackId,
				elementId: ref.element.id,
				updates: {
					transform: { ...ref.element.transform, ...updates },
				},
			})),
			pushHistory,
		});
	};

''',
    marker="每个片段按自己的当前速度换算时长",
)

# 透明度那 6 处内联更新也改成批量
patch_re(
    VIDEO_PROPERTIES,
    r"editor\.timeline\.updateElements\(\{\s*updates: \[\s*\{\s*trackId,\s*elementId: element\.id,"
    r"\s*updates: \{ opacity: (.+?) \},\s*\},\s*\],\s*pushHistory: (true|false),\s*\}\);",
    r"updateBulk({ opacity: \1 }, \2);",
    expect=6,
)


# ---------------------------------------------------- 音频属性：多选
patch_text(
    AUDIO_PROPERTIES,
    'import { useEffect, useMemo, useReducer, useRef, useState } from "react";',
    [
        (
            'import { useEffect, useReducer, useRef, useState } from "react";',
            'import { useEffect, useMemo, useReducer, useRef, useState } from "react";',
            1,
        ),
    ],
)

replace_between(
    AUDIO_PROPERTIES,
    "export function AudioProperties({",
    "\tconst [beautifyBusy, setBeautifyBusy] = useState(false);",
    r'''export function AudioProperties({
	elements: elementRefs,
}: {
	elements: { element: AudioElement; trackId: string }[];
}) {
	// 数值显示取第一个选中片段；修改分发到全部选中片段（多选 = 集体操作）
	const element = elementRefs[0].element;
	const { t } = useTranslation();
	const editor = useEditor();
	const [, forceRender] = useReducer((x: number) => x + 1, 0);
	const uploadRefs = useMemo(
		() =>
			elementRefs.filter(
				(ref) => (ref.element as UploadAudioElement).sourceType === "upload",
			) as { element: UploadAudioElement; trackId: string }[],
		[elementRefs],
	);
''',
    marker="elements: { element: AudioElement; trackId: string }[];",
)

replace_between(
    AUDIO_PROPERTIES,
    "\tconst updateElement = ({",
    "\tconst applySpeedChange = ({",
    r'''	const updateRefs = (
		refs: { element: AudioElement; trackId: string }[],
		updates: Partial<Record<string, unknown>>,
		pushHistory = true,
	) => {
		if (refs.length === 0) return;
		editor.timeline.updateElements({
			updates: refs.map((ref) => ({
				trackId: ref.trackId,
				elementId: ref.element.id,
				updates,
			})),
			pushHistory,
		});
	};

	const updateElement = ({
		updates,
		pushHistory = true,
	}: {
		updates: Partial<Record<string, unknown>>;
		pushHistory?: boolean;
	}) => {
		updateRefs(elementRefs, updates, pushHistory);
	};

''',
    marker="const updateRefs = (\n\t\trefs: { element: AudioElement; trackId: string }[]",
)

replace_between(
    AUDIO_PROPERTIES,
    "\tconst applySpeedChange = ({",
    "\tconst beautifySourceId =",
    r'''	const applySpeedChange = ({
		newRate,
		pushHistory,
	}: {
		newRate: number;
		pushHistory: boolean;
	}) => {
		// 每个片段按自己的当前速度换算时长：多选就是一次性全改
		editor.timeline.updateElements({
			updates: elementRefs.map((ref) => {
				const oldRate = ref.element.playbackRate ?? 1;
				return {
					trackId: ref.trackId,
					elementId: ref.element.id,
					updates: {
						playbackRate: newRate,
						duration: ref.element.duration * (oldRate / newRate),
					},
				};
			}),
			pushHistory,
		});
	};

''',
    marker="// 每个片段按自己的当前速度换算时长：多选就是一次性全改",
)

patch_text(
    AUDIO_PROPERTIES,
    "// 勾选框反映整组状态：全部勾上才算勾上",
    [
        (
            "\tconst beautifySourceId =\n"
            "\t\t(element as UploadAudioElement).voiceBeautifySourceId ??\n"
            "\t\t(element as UploadAudioElement).mediaId;\n"
            "\tconst beautifyStoredEnabled =\n"
            "\t\t(element as UploadAudioElement).voiceBeautifyEnabled ??\n"
            "\t\tBoolean((element as UploadAudioElement).voiceBeautifySourceId);",
            "\t// 勾选框反映整组状态：全部勾上才算勾上\n"
            "\tconst beautifyStoredEnabled =\n"
            "\t\tuploadRefs.length > 0 &&\n"
            "\t\tuploadRefs.every(\n"
            "\t\t\t(ref) =>\n"
            "\t\t\t\tref.element.voiceBeautifyEnabled ??\n"
            "\t\t\t\tBoolean(ref.element.voiceBeautifySourceId),\n"
            "\t\t);",
            1,
        ),
    ],
)


replace_between(
    AUDIO_PROPERTIES,
    "\tconst runVocalBeautify = async () => {",
    '\treturn (\n\t\t<div className="flex h-full flex-col">',
    r'''	const runVocalBeautify = async () => {
		if (beautifyBusy) return;
		if (uploadRefs.length === 0) {
			setBeautifyOverride(null);
			setBeautifyStatus("所选片段没有可处理的音频素材");
			return;
		}
		// 同一份素材只处理一次，结果给所有引用它的片段共用
		const groups = new Map<
			string,
			{ element: UploadAudioElement; trackId: string }[]
		>();
		for (const ref of uploadRefs) {
			const key = ref.element.voiceBeautifySourceId ?? ref.element.mediaId;
			const list = groups.get(key) ?? [];
			list.push(ref);
			groups.set(key, list);
		}
		setBeautifyBusy(true);
		setBeautifyProgress(0);
		setBeautifyStatus("准备中…");
		let index = 0;
		let processed = 0;
		let skipped = 0;
		try {
			for (const [sourceId, refs] of groups) {
				index += 1;
				const prefix =
					groups.size > 1 ? `第 ${index}/${groups.size} 个素材 · ` : "";
				const source = editor.media
					.getAssets()
					.find((asset) => asset.id === sourceId);
				if (!source) {
					skipped += refs.length;
					continue;
				}
				const result = await applyVocalBeautify({
					editor,
					source,
					strength: beautifyStrength,
					onProgress: (state) => {
						setBeautifyStatus(`${prefix}${describeVocalBeautify(state)}`);
						setBeautifyProgress(
							((index - 1) + (state.progress ?? 0)) / groups.size,
						);
					},
				});
				updateRefs(
					refs,
					{
						mediaId: result.mediaId,
						name: result.name,
						voiceBeautifyEnabled: true,
						voiceBeautifyMediaId: result.mediaId,
						voiceBeautifySourceId: sourceId,
						voiceBeautifySourceName:
							refs[0].element.voiceBeautifySourceName ?? refs[0].element.name,
						voiceBeautifyStrength: beautifyStrength,
					},
					true,
				);
				processed += refs.length;
			}
			setBeautifyStatus(
				skipped > 0
					? `已处理 ${processed} 个片段，${skipped} 个跳过`
					: `已处理 ${processed} 个片段：预览与导出都用处理后的音频`,
			);
		} catch (error) {
			setBeautifyOverride(null);
			setBeautifyStatus(`失败：${(error as Error).message}`);
		} finally {
			setBeautifyBusy(false);
			setBeautifyProgress(null);
		}
	};

	const toggleVocalBeautify = (checked: boolean) => {
		if (beautifyBusy) return;
		setBeautifyOverride(checked);
		if (uploadRefs.length === 0) {
			setBeautifyOverride(null);
			setBeautifyStatus("所选片段没有可处理的音频素材");
			return;
		}
		if (!checked) {
			// 取消勾选：整组回到原始素材
			editor.timeline.updateElements({
				updates: uploadRefs.map((ref) => ({
					trackId: ref.trackId,
					elementId: ref.element.id,
					updates: {
						mediaId: ref.element.voiceBeautifySourceId ?? ref.element.mediaId,
						name:
							ref.element.voiceBeautifySourceName ??
							ref.element.name.replace(/-人声美化(?=\.|$)/, ""),
						voiceBeautifyEnabled: false,
					},
				})),
				pushHistory: true,
			});
			setBeautifyStatus("");
			return;
		}
		if (uploadRefs.every((ref) => Boolean(ref.element.voiceBeautifyMediaId))) {
			editor.timeline.updateElements({
				updates: uploadRefs.map((ref) => ({
					trackId: ref.trackId,
					elementId: ref.element.id,
					updates: {
						mediaId: ref.element.voiceBeautifyMediaId,
						name:
							editor.media
								.getAssets()
								.find(
									(asset) => asset.id === ref.element.voiceBeautifyMediaId,
								)?.name ?? ref.element.name,
						voiceBeautifyEnabled: true,
					},
				})),
				pushHistory: true,
			});
			setBeautifyStatus("");
			return;
		}
		void runVocalBeautify();
	};

''',
    marker="第 ${index}/${groups.size} 个素材 · ",
)


# ---------------------------------------------------- 贴纸属性：多选
replace_between(
    STICKER_PROPERTIES,
    "export function StickerProperties({",
    "\tconst { t } = useTranslation();",
    r'''type StickerElementRef = { element: StickerElement; trackId: string };

export function StickerProperties({
	elements: elementRefs,
}: {
	elements: StickerElementRef[];
}) {
	// 数值显示取第一个选中片段；修改分发到全部选中片段（多选 = 集体操作）
	const element = elementRefs[0].element;
''',
    marker="elements: StickerElementRef[];",
)

replace_between(
    STICKER_PROPERTIES,
    "\tconst updateElement = ({",
    "\tconst commitNumberField = ({",
    r'''	const updateRefs = (
		refs: StickerElementRef[],
		updates: Partial<Record<string, unknown>>,
		pushHistory = true,
	) => {
		if (refs.length === 0) return;
		editor.timeline.updateElements({
			updates: refs.map((ref) => ({
				trackId: ref.trackId,
				elementId: ref.element.id,
				updates,
			})),
			pushHistory,
		});
	};

	const updateElement = ({
		updates,
		pushHistory = true,
	}: {
		updates: Partial<Record<string, unknown>>;
		pushHistory?: boolean;
	}) => {
		updateRefs(elementRefs, updates, pushHistory);
	};

	const updateTransform = ({
		updates,
		pushHistory = true,
	}: {
		updates: Partial<typeof element.transform>;
		pushHistory?: boolean;
	}) => {
		editor.timeline.updateElements({
			updates: elementRefs.map((ref) => ({
				trackId: ref.trackId,
				elementId: ref.element.id,
				updates: {
					transform: { ...ref.element.transform, ...updates },
				},
			})),
			pushHistory,
		});
	};

''',
    marker="const updateRefs = (\n\t\trefs: StickerElementRef[]",
)


# =====================================================================
# 字幕转录：内置 MOSS 引擎 + 权重缺失自动下载（前端显示下载进度）
# ---------------------------------------------------------------------
# 后端（yuancut_transcribe.py + yuancut_asr/）已经不再依赖 custom_nodes 下的
# ComfyUI-Moss-Diarize-Yuan 节点包：引擎和下载器都搬进了插件。
# 这里补前端：转写前先问一下 `/yuancut/transcribe` 的状态，权重没就绪就把
# 「模型下载中 x%」回显到面板上，而不是让用户看到一条 "找不到引擎" 的红字。
# =====================================================================

WORKER_RUNTIME = SRC / "services" / "transcription" / "worker-runtime.js"

patch_text(
    WORKER_RUNTIME,
    "const STATUS_ENDPOINT = TRANSCRIBE_ENDPOINT;",
    [
        (
            "const MAX_WINDOW_SECONDS = 300;\n",
            "const MAX_WINDOW_SECONDS = 300;\n"
            "// 同一个路由的 GET 返回引擎 / 权重状态（是否就绪、下载进度）\n"
            "const STATUS_ENDPOINT = TRANSCRIBE_ENDPOINT;\n",
            1,
        ),
        (
            "\ttry {\n\t\tfor (let windowIndex = 0; windowIndex < windowCount; windowIndex++) {",
            "\ttry {\n"
            "\t\t// 首次使用时要先等权重就绪（1.8 GB，下载进度会回显到界面）\n"
            "\t\tawait waitForModel();\n"
            "\n"
            "\t\tfor (let windowIndex = 0; windowIndex < windowCount; windowIndex++) {",
            1,
        ),
        (
            "\t} finally {\n\t\tactiveController = null;\n\t}\n}\n",
            "\t} finally {\n\t\tactiveController = null;\n\t}\n}\n"
            "\n"
            "/**\n"
            " * 等服务端权重就绪。\n"
            " * 没说就绪就每 2 秒查一次状态，把下载进度当成「转写进度」发回去（带 label），\n"
            " * 界面会显示「模型下载中 x%（a/b GiB）」。下完直接返回，继续正常转写。\n"
            " */\n"
            "async function waitForModel() {\n"
            "\tfor (let attempt = 0; attempt < 900; attempt++) {\n"
            "\t\tif (cancelled) return;\n"
            "\t\tlet status = null;\n"
            "\t\ttry {\n"
            "\t\t\tconst response = await fetch(STATUS_ENDPOINT, { cache: \"no-store\" });\n"
            "\t\t\tif (response.ok) status = await response.json();\n"
            "\t\t} catch {\n"
            "\t\t\t// 状态接口问不到就让 POST 自己去报错，别卡住转写\n"
            "\t\t\treturn;\n"
            "\t\t}\n"
            "\t\tif (!status || status.model_ready) return;\n"
            "\t\tif (status.download_error) {\n"
            "\t\t\tthrow new Error(\"模型下载失败：\" + status.download_error);\n"
            "\t\t}\n"
            "\t\tconst done = Math.max(0, Math.min(1, Number(status.download_progress) || 0));\n"
            "\t\tconst gib = (Number(status.downloaded_bytes) || 0) / 1024 ** 3;\n"
            "\t\tconst total = (Number(status.total_bytes) || 0) / 1024 ** 3;\n"
            "\t\tself.postMessage({\n"
            "\t\t\ttype: \"transcribe-progress\",\n"
            "\t\t\tprogress: Math.round(done * 100),\n"
            "\t\t\tlabel:\n"
            "\t\t\t\ttotal > 0\n"
            "\t\t\t\t\t? \"模型下载中 \" +\n"
            "\t\t\t\t\t  Math.round(done * 100) +\n"
            "\t\t\t\t\t  \"%\uff08\" +\n"
            "\t\t\t\t\t  gib.toFixed(2) +\n"
            "\t\t\t\t\t  \"/\" +\n"
            "\t\t\t\t\t  total.toFixed(2) +\n"
            "\t\t\t\t\t  \" GiB\uff09\"\n"
            "\t\t\t\t\t: \"模型下载中 \" + Math.round(done * 100) + \"%\",\n"
            "\t\t});\n"
            "\t\tawait new Promise((resolve) => setTimeout(resolve, 2000));\n"
            "\t}\n"
            "}\n",
            1,
        ),
    ],
)
print("  patched worker-runtime.js（转写前等权重就绪 + 下载进度回显）")

patch_text(
    SRC / "services" / "transcription" / "worker.ts",
    "| { type: \"transcribe-progress\"; progress: number; label?: string }",
    [
        (
            '\t| { type: "transcribe-progress"; progress: number }',
            "\t| { type: \"transcribe-progress\"; progress: number; label?: string }",
            1,
        ),
    ],
)

patch_text(
    SRC / "types" / "transcription.ts",
    '"downloading-model"',
    [
        (
            '\t| "loading-model"\n\t| "transcribing"',
            '\t| "loading-model"\n\t| "downloading-model"\n\t| "transcribing"',
            1,
        ),
    ],
)

patch_text(
    SRC / "services" / "transcription" / "service.ts",
    'status: response.label ? "downloading-model" : "transcribing",',
    [
        (
            '\t\t\t\t\tcase "transcribe-progress":\n'
            "\t\t\t\t\t\tonProgress?.({\n"
            '\t\t\t\t\t\t\tstatus: "transcribing",\n'
            "\t\t\t\t\t\t\tprogress: response.progress,\n"
            "\t\t\t\t\t\t});",
            '\t\t\t\t\tcase "transcribe-progress":\n'
            "\t\t\t\t\t\tonProgress?.({\n"
            '\t\t\t\t\t\t\t// 带 label 的是「权重还在下载」，不是真正的转写进度\n'
            '\t\t\t\t\t\t\tstatus: response.label ? "downloading-model" : "transcribing",\n'
            "\t\t\t\t\t\t\tprogress: response.progress,\n"
            "\t\t\t\t\t\t\tmessage: response.label,\n"
            "\t\t\t\t\t\t});",
            1,
        ),
    ],
)

patch_text(
    SRC / "components" / "editor" / "panels" / "assets" / "views" / "captions.tsx",
    'progress.status === "downloading-model"',
    [
        (
            '\t\t} else if (progress.status === "transcribing") {',
            "\t\t} else if (progress.status === \"downloading-model\") {\n"
            "\t\t\t// 首次使用：服务端在自动下载 1.8 GB 权重\n"
            "\t\t\tsetProgressValue(overall);\n"
            "\t\t\tsetProcessingStep(progress.message || t(\"Downloading model...\"));\n"
            '\t\t} else if (progress.status === "transcribing") {',
            1,
        ),
    ],
)
