import json
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
import zipfile
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent

FILES = {
    "teacher_video": "389951149018281_28272875_teacher.mp4",
    "student_video": "389951149018281_38208281_student_L5.mp4",
    "class_video": "Screen-2023-05-18-143434.mp4",
    "label_json": "MC-L1-U10-LC2-5 打标 Json.json",
    "eventinfo": "MC-L3-U4-LC1-3.eventinfo.389775021016422.txt",
    "long_text": "[已解密]_long_text_2023-05-09-18-39-50.txt",
    "docx": "1v1信令消息格式.docx",
}


def read_text_any(path: Path) -> str:
    encodings = ["utf-8", "utf-16", "utf-16le", "utf-16be", "gb18030", "latin1"]
    data = path.read_bytes()
    for enc in encodings:
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("latin1", errors="ignore")


def load_json(path: Path) -> Any:
    text = read_text_any(path)
    return json.loads(text)


def summarize_basic(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        keys = list(obj.keys())
        return {
            "type": "dict",
            "keys": keys[:200],
            "keys_count": len(keys),
        }
    if isinstance(obj, list):
        types = Counter(type(x).__name__ for x in obj)
        sample = obj[0] if obj else None
        summary = {
            "type": "list",
            "length": len(obj),
            "element_types": dict(types),
        }
        if isinstance(sample, dict):
            summary["sample_keys"] = list(sample.keys())[:200]
        return summary
    return {"type": type(obj).__name__}


TIME_KEY_RE = re.compile(r"(time|timestamp|start|end|duration|begin|finish|offset)", re.I)


def collect_key_stats(obj: Any) -> Dict[str, Dict[str, Any]]:
    stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "types": Counter(), "samples": []})

    def visit(x: Any):
        if isinstance(x, dict):
            for k, v in x.items():
                st = stats[k]
                st["count"] += 1
                st["types"][type(v).__name__] += 1
                if len(st["samples"]) < 3:
                    st["samples"].append(v)
                visit(v)
        elif isinstance(x, list):
            for v in x:
                visit(v)

    visit(obj)
    return stats


def extract_time_fields(stats: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {k: v for k, v in stats.items() if TIME_KEY_RE.search(k)}


def parse_eventinfo(path: Path) -> Dict[str, Any]:
    outer = load_json(path)
    data = outer.get("data")
    inner = json.loads(data) if isinstance(data, str) else data
    events = (inner or {}).get("events") or {}
    vk = events.get("vk") or []
    type_counts = Counter()
    msgid_counts = Counter()
    role_counts = Counter()
    user_counts = Counter()
    time_values = []

    for ev in vk:
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype is not None:
            type_counts[str(etype)] += 1
        for tkey in ["recordTime", "createTime", "clientEventCreateTime"]:
            tv = ev.get(tkey)
            if isinstance(tv, (int, float)):
                time_values.append(int(tv))
        args = ev.get("arguments") or {}
        if isinstance(args, dict):
            if "msgid" in args:
                msgid_counts[str(args.get("msgid"))] += 1
            if "role" in args:
                role_counts[str(args.get("role"))] += 1
            if "userid" in args:
                user_counts[str(args.get("userid"))] += 1
            ts = args.get("timestamp")
            if isinstance(ts, str):
                m = re.match(r"(\\d+)", ts)
                if m:
                    time_values.append(int(m.group(1)))

    time_values = sorted(set(time_values))
    time_range = None
    if time_values:
        time_range = {
            "min": time_values[0],
            "max": time_values[-1],
            "count": len(time_values),
        }

    return {
        "outer_keys": list(outer.keys()),
        "inner_keys": list((inner or {}).keys()),
        "events_keys": list(events.keys()) if isinstance(events, dict) else [],
        "vk_length": len(vk) if isinstance(vk, list) else None,
        "type_counts": dict(type_counts.most_common(20)),
        "msgid_counts_top": dict(msgid_counts.most_common(30)),
        "role_counts": dict(role_counts.most_common(10)),
        "user_counts": dict(user_counts.most_common(10)),
        "time_range": time_range,
        "sample_event_keys": list(vk[0].keys()) if isinstance(vk, list) and vk else [],
        "sample_arguments_keys": list((vk[0].get("arguments") or {}).keys())[:50]
        if isinstance(vk, list) and vk
        else [],
    }


def parse_long_text(path: Path) -> Dict[str, Any]:
    obj = load_json(path)
    summary = summarize_basic(obj)
    sections = Counter()
    functions = Counter()
    slide_nos = []
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                sections[str(item.get("Section"))] += 1
                functions[str(item.get("Function"))] += 1
                slide = item.get("SlideNo")
                if slide is not None:
                    slide_nos.append(slide)
    return {
        "summary": summary,
        "sections_top": dict(sections.most_common(30)),
        "functions_top": dict(functions.most_common(30)),
        "slide_count": len(slide_nos),
        "slide_samples": slide_nos[:10],
        "key_stats": extract_time_fields(collect_key_stats(obj)),
    }


def parse_label_json(path: Path) -> Dict[str, Any]:
    obj = load_json(path)
    summary = summarize_basic(obj)
    stats = collect_key_stats(obj)
    time_fields = extract_time_fields(stats)
    return {
        "summary": summary,
        "time_fields": {k: {"count": v["count"], "types": dict(v["types"]), "samples": v["samples"]} for k, v in time_fields.items()},
        "top_keys": list(stats.keys())[:200],
    }


def parse_docx(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"exists": False}
    text = ""
    with zipfile.ZipFile(path, "r") as zf:
        if "word/document.xml" in zf.namelist():
            xml_data = zf.read("word/document.xml")
            root = ET.fromstring(xml_data)
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            parts = []
            for node in root.findall(".//w:t", ns):
                if node.text:
                    parts.append(node.text)
            text = "".join(parts)
    sample = text[:1200]
    keywords = {}
    for kw in ["msgid", "room", "role", "timestamp", "signal", "message", "event", "join", "leave"]:
        keywords[kw] = text.lower().count(kw)
    return {
        "exists": True,
        "text_len": len(text),
        "sample": sample,
        "keyword_counts": keywords,
    }


def ffprobe_media(path: Path) -> Dict[str, Any]:
    if not shutil.which("ffprobe"):
        return {"available": False}
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        info = json.loads(out.decode("utf-8", errors="ignore"))
    except Exception as e:
        return {"available": True, "error": str(e)}

    streams = info.get("streams", [])
    fmt = info.get("format", {})
    stream_summaries = []
    for s in streams:
        ssum = {
            "codec_type": s.get("codec_type"),
            "codec_name": s.get("codec_name"),
            "width": s.get("width"),
            "height": s.get("height"),
            "r_frame_rate": s.get("r_frame_rate"),
            "sample_rate": s.get("sample_rate"),
            "channels": s.get("channels"),
            "channel_layout": s.get("channel_layout"),
        }
        stream_summaries.append(ssum)
    return {
        "available": True,
        "format": {
            "duration": fmt.get("duration"),
            "size": fmt.get("size"),
            "bit_rate": fmt.get("bit_rate"),
            "start_time": fmt.get("start_time"),
            "tags": fmt.get("tags", {}),
        },
        "streams": stream_summaries,
    }


def human_dt(ms: int) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000.0).isoformat()
    except Exception:
        return ""


def build_report(data: Dict[str, Any], output_path: Path) -> None:
    lines: List[str] = []
    lines.append("# VIPKID 数据分析与可行性报告")
    lines.append("")
    lines.append("## 文件清单")
    lines.append("")
    for name, info in data["files"].items():
        lines.append(f"- `{name}`: {info}")
    lines.append("")

    lines.append("## 课程内容/课件结构（long_text）")
    lt = data.get("long_text", {})
    lines.append(f"- 结构概览: {lt.get('summary')}")
    lines.append(f"- Section 统计(前30): {lt.get('sections_top')}")
    lines.append(f"- Function 统计(前30): {lt.get('functions_top')}")
    lines.append(f"- Slide 数量: {lt.get('slide_count')}, 示例: {lt.get('slide_samples')}")
    lines.append(f"- 时间相关字段: {lt.get('key_stats')}")
    lines.append("")

    lines.append("## 打标/标注数据（label_json）")
    lj = data.get("label_json", {})
    lines.append(f"- 结构概览: {lj.get('summary')}")
    lines.append(f"- 时间相关字段: {lj.get('time_fields')}")
    lines.append(f"- 顶层字段示例: {lj.get('top_keys')}")
    lines.append("")

    lines.append("## 事件日志（eventinfo）")
    ei = data.get("eventinfo", {})
    lines.append(f"- outer_keys: {ei.get('outer_keys')}")
    lines.append(f"- inner_keys: {ei.get('inner_keys')}")
    lines.append(f"- events_keys: {ei.get('events_keys')}")
    lines.append(f"- vk_length: {ei.get('vk_length')}")
    lines.append(f"- type_counts: {ei.get('type_counts')}")
    lines.append(f"- msgid_counts_top: {ei.get('msgid_counts_top')}")
    lines.append(f"- role_counts: {ei.get('role_counts')}")
    lines.append(f"- user_counts: {ei.get('user_counts')}")
    tr = ei.get("time_range")
    if isinstance(tr, dict):
        lines.append(f"- time_range(ms): {tr} (min_dt={human_dt(tr['min'])}, max_dt={human_dt(tr['max'])})")
    lines.append("")

    lines.append("## 1v1信令消息格式（docx）")
    dx = data.get("docx", {})
    lines.append(f"- text_len: {dx.get('text_len')}")
    lines.append(f"- keyword_counts: {dx.get('keyword_counts')}")
    sample = dx.get("sample") or ""
    if sample:
        lines.append("- sample:")
        lines.append("```")
        lines.append(sample)
        lines.append("```")
    lines.append("")

    lines.append("## 视频元数据（ffprobe）")
    for key in ["teacher_video", "student_video", "class_video"]:
        lines.append(f"- {key}: {data.get('media', {}).get(key)}")
    lines.append("")

    lines.append("## 可对齐的时间轴线索")
    lines.append("- eventinfo 提供 recordTime/createTime/arguments.timestamp，可作为课堂事件时间轴。")
    lines.append("- 标注 JSON 若包含 start/end/timestamp，可与事件时间轴和视频对齐。")
    lines.append("- 三段视频需通过 ffprobe 的 duration/start_time 与事件时间范围手动或自动对齐。")
    lines.append("- long_text 是课件结构，可作为课堂阶段/教学意图标签。")
    lines.append("")

    lines.append("## AI Tutor 功能可行性分析（基于当前数据）")
    lines.append("### 1) 学习老师互动与课堂节奏")
    lines.append("- 可用数据: 老师视频 + 课堂屏幕录像 + 课件脚本/结构。")
    lines.append("- 需要补齐: 教师语音转写(ASR)、学生回应转写、发言轮次对齐、关键教学动作标注。")
    lines.append("- 可行性: 中等，需先做多模态对齐和话轮抽取。")
    lines.append("")
    lines.append("### 2) 学生表情/语音情绪识别")
    lines.append("- 可用数据: 学生视频(面部/语音)。")
    lines.append("- 需要补齐: 情绪/专注度标签或弱监督信号；多堂课/多学生数据以提升泛化。")
    lines.append("- 可行性: 中等偏低，单堂课难以训练稳健模型。")
    lines.append("")
    lines.append("### 3) 实时反馈与教学策略调整")
    lines.append("- 可用数据: 课堂事件日志(信令)、视频与课件结构。")
    lines.append("- 需要补齐: 低延迟 ASR、实时状态估计、教学策略规则/策略标注。")
    lines.append("- 可行性: 需要工程与数据双投入，现有数据更偏离线研究。")
    lines.append("")
    lines.append("### 4) 课堂后评价与建议")
    lines.append("- 可用数据: 课件结构 + 学生/老师视频 + 事件日志。")
    lines.append("- 需要补齐: 学习目标达成度、答题正确率/完成度、课堂表现评分口径。")
    lines.append("- 可行性: 中等，可先通过规则+LLM模板生成，再逐步优化。")
    lines.append("")
    lines.append("### 5) 中长期学习画像")
    lines.append("- 可用数据: 单堂课不足以建立画像。")
    lines.append("- 需要补齐: 多堂课历史数据、统一学生ID、阶段性测评、课后作业与成绩。")
    lines.append("- 可行性: 需要长期数据累积与隐私合规策略。")
    lines.append("")

    lines.append("## 关键缺口与建议")
    lines.append("- 语音转写(老师/学生)与话轮分割是核心基础。")
    lines.append("- 多堂课、多学生的数据规模不足以训练情绪/专注模型。")
    lines.append("- 需要统一时间轴与对齐策略(事件日志/视频/标注/课件)。")
    lines.append("- 建议建立标准化标注规范：课堂阶段、互动类型、学生状态、教师策略。")
    lines.append("- 注意隐私合规：人脸、语音与儿童数据需严格授权与脱敏。")
    lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    files_info = {}
    for key, fname in FILES.items():
        path = ROOT / fname
        if path.exists():
            files_info[key] = f"{fname} ({path.stat().st_size} bytes)"
        else:
            files_info[key] = f"{fname} (missing)"

    data = {"files": files_info}

    lt_path = ROOT / FILES["long_text"]
    if lt_path.exists():
        data["long_text"] = parse_long_text(lt_path)

    lj_path = ROOT / FILES["label_json"]
    if lj_path.exists():
        data["label_json"] = parse_label_json(lj_path)

    ei_path = ROOT / FILES["eventinfo"]
    if ei_path.exists():
        data["eventinfo"] = parse_eventinfo(ei_path)

    dx_path = ROOT / FILES["docx"]
    if dx_path.exists():
        data["docx"] = parse_docx(dx_path)

    media = {}
    for key in ["teacher_video", "student_video", "class_video"]:
        mpath = ROOT / FILES[key]
        if mpath.exists():
            media[key] = ffprobe_media(mpath)
        else:
            media[key] = {"available": False, "error": "missing"}
    data["media"] = media

    out_path = ROOT / "report.md"
    build_report(data, out_path)
    print(f"Report written to {out_path}")


if __name__ == "__main__":
    main()
