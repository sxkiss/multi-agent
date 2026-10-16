"""浏览器录制回放 + 生成技能（对齐 Octop api/routers/browser/record_replay.py）。

  GET  /api/v2/browser/record-replay/status
  POST /api/v2/browser/record-replay/start
  POST /api/v2/browser/record-replay/stop
  POST /api/v2/browser/record-replay/stop-and-generate-skill
  POST /api/v2/browser/record-replay/skill-content
  POST /api/v2/browser/record-replay/replay
  GET  /api/v2/browser/record-replay/recordings
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import public
from platform_ext.browser.harness import DEFAULT_PROFILE

logger = logging.getLogger("platform-ext-browser-record")

router = APIRouter()

# 内存中的活动录制会话（单用户）
_ACTIVE: dict[str, Any] = {"session": None, "recording_id": None, "profile": None}


class StartBody(BaseModel):
    profile: str = DEFAULT_PROFILE
    name: str = ""


class ReplayBody(BaseModel):
    recording_id: str
    profile: str = DEFAULT_PROFILE
    inputs: dict[str, str] = {}


class RecordingIdBody(BaseModel):
    recording_id: str


def _store():
    from octop_browser.record import RecordingStore  # noqa: PLC0415
    return RecordingStore()


def _ok(data: Any = None, msg: str = "") -> JSONResponse:
    return JSONResponse(public.return_data(True, data=data, msg=msg))


def _err(msg: str) -> JSONResponse:
    return JSONResponse(public.returnMsg(False, msg))


@router.get("/browser/record-replay/status")
async def record_status() -> JSONResponse:
    store = _store()
    active = _ACTIVE.get("session") is not None
    return _ok({
        "active": active,
        "recording_id": _ACTIVE.get("recording_id"),
        "profile": _ACTIVE.get("profile"),
        "recordings": [m.model_dump() if hasattr(m, "model_dump") else m
                       for m in store.list_recordings()],
    })


@router.get("/browser/record-replay/recordings")
async def list_recordings() -> JSONResponse:
    store = _store()
    out = []
    for m in store.list_recordings():
        rid = getattr(m, "recordingId", None) or getattr(m, "recording_id", "")
        try:
            summary = store.recording_summary(rid)
        except Exception:  # noqa: BLE001
            summary = {}
        out.append({"recording_id": rid, "summary": summary})
    return _ok({"recordings": out})


@router.post("/browser/record-replay/start")
async def record_start(body: StartBody) -> JSONResponse:
    if _ACTIVE.get("session") is not None:
        return _err("已有录制进行中")
    try:
        from octop_browser.record import RecordController  # noqa: PLC0415
        ctrl = RecordController()
        session, info = await ctrl.start_in_process(profile=body.profile, name=body.name)
        _ACTIVE.update({"session": session, "recording_id": info.get("recordingId"),
                        "profile": body.profile, "controller": ctrl})
        return _ok(info, "已开始录制")
    except Exception as exc:  # noqa: BLE001
        logger.exception("record start failed")
        return _err(str(exc))


@router.post("/browser/record-replay/stop")
async def record_stop() -> JSONResponse:
    session = _ACTIVE.get("session")
    if session is None:
        return _err("没有进行中的录制")
    try:
        manifest = await session.stop()
        rid = getattr(manifest, "recordingId", None) or _ACTIVE.get("recording_id")
        _ACTIVE.update({"session": None, "recording_id": None})
        return _ok({"recording_id": rid}, "已停止录制")
    except Exception as exc:  # noqa: BLE001
        logger.exception("record stop failed")
        _ACTIVE.update({"session": None, "recording_id": None})
        return _err(str(exc))


@router.post("/browser/record-replay/stop-and-generate-skill")
async def stop_and_generate_skill(body: RecordingIdBody | None = None) -> JSONResponse:
    """停止录制 → 生成步骤 → 生成技能草稿。"""
    rid = None
    session = _ACTIVE.get("session")
    if session is not None:
        try:
            manifest = await session.stop()
            rid = getattr(manifest, "recordingId", None) or _ACTIVE.get("recording_id")
        except Exception as exc:  # noqa: BLE001
            logger.warning("stop during generate failed: %s", exc)
            rid = _ACTIVE.get("recording_id")
        _ACTIVE.update({"session": None, "recording_id": None})
    elif body is not None:
        rid = body.recording_id
    if not rid:
        return _err("缺少 recording_id")

    try:
        from octop_browser.record import SemanticProcessor, SkillGenerator  # noqa: PLC0415
        store = _store()
        events = store.read_events(rid)
        steps = SemanticProcessor(rid, events).process()
        store.write_steps(rid, steps)
        skill_md = SkillGenerator(steps).generate()
        path = store.write_skill(rid, skill_md, draft=True)
        return _ok({"recording_id": rid, "skill_path": str(path),
                      "skill_content": skill_md, "steps": len(steps.steps)})
    except Exception as exc:  # noqa: BLE001
        logger.exception("generate skill failed")
        return _err(str(exc))


@router.post("/browser/record-replay/skill-content")
async def get_skill_content(body: RecordingIdBody) -> JSONResponse:
    try:
        store = _store()
        p = store.skill_path(body.recording_id, draft=True)
        if not p.exists():
            return _err("技能草稿不存在，请先生成")
        return _ok({"skill_content": p.read_text(encoding="utf-8")})
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


@router.post("/browser/record-replay/replay")
async def replay(body: ReplayBody) -> JSONResponse:
    try:
        from octop_browser.record import ReplayRunner  # noqa: PLC0415
        runner = ReplayRunner()
        report = await runner.run(body.recording_id, profile=body.profile, inputs=body.inputs)
        return _ok({"report": report})
    except Exception as exc:  # noqa: BLE001
        logger.exception("replay failed")
        return _err(str(exc))