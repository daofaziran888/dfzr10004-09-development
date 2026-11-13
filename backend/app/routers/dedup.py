"""采集去重比对接口：维护去重策略，覆盖提交校验、启用策略、停用策略等动作。

去重预演入口也在这里：发起预演、查看批次与重复组、一次提交人工裁决、汇总一致性。
注意静态路径（/rehearsals、/export）必须放在 /{entry_id} 之前注册，否则会被
整数路径参数拦截返回 422。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dedup import DedupRehearsalService, DedupService

router = APIRouter(prefix="/api/dedup", tags=["采集去重比对"])

service = DedupService()
rehearsal_service = DedupRehearsalService()

LIST_FIELDS = ["策略编号", "业务键列", "排序字段", "保留策略", "时间窗口", "相似阈值", "比对字段", "是否启用"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.post("/rehearsals", response_model=ActionResult)
def run_rehearsal(payload: EntryPayload) -> ActionResult:
    """发起去重预演：按时间窗口与比对字段计算重复组，给出拟保留记录与排除依据。

    空窗口会明确说明没有可比记录；同一业务键在同一窗口的回执再次送达时只保留
    先到的去重组，并发提交同一业务键时先到者生效，组仍按各自时间窗口隔离。
    """
    batch, message = rehearsal_service.run_rehearsal(payload.values)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/rehearsals", response_model=PageResult[dict])
def list_rehearsals(page: int = 1, size: int = 20) -> PageResult[dict]:
    """预演批次列表：按发起时间倒序，最近的批次排在前面。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = rehearsal_service.list_rehearsals(page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/rehearsals/{batch_id}", response_model=dict)
def get_rehearsal(batch_id: int) -> dict:
    """预演批次详情：批次概要加全部重复组（含拟保留、排除记录与裁决依据）。"""
    detail = rehearsal_service.get_rehearsal(batch_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"预演批次 {batch_id} 不存在")
    return detail


@router.post("/rehearsals/{batch_id}/adjudicate", response_model=ActionResult)
def adjudicate_rehearsal(batch_id: int, payload: EntryPayload) -> ActionResult:
    """一次提交人工裁决：支持按重复组批量处理、只处理其中一条或整组回执。

    人工复核裁定具有最高优先级：结论回写同一去重批次，同步更新采集数据标注
    待办，并写入人工裁定档案，后续预演不得用策略结果覆盖。
    """
    decisions = payload.values.get("decisions") or []
    if not isinstance(decisions, list) or not decisions:
        return ActionResult(ok=False, message="未提交任何裁决，请至少选择一条记录或一个重复组")
    reviewer = str(payload.values.get("裁决人") or "复核员").strip() or "复核员"
    batch, results = rehearsal_service.adjudicate(batch_id, decisions, reviewer)
    if batch is None:
        return ActionResult(ok=False, message=f"预演批次 {batch_id} 不存在")
    succeeded = sum(1 for item in results if item.get("ok"))
    message = (
        f"批次 {batch['预演编号']} 一次提交 {len(results)} 条裁决，生效 {succeeded} 条；"
        f"待复核 {batch['待复核组数']} 组，拟保留 {batch['拟保留数']} 条，已排除 {batch['已排除数']} 条"
    )
    return ActionResult(ok=True, message=message, entry={"batch": batch, "results": results})


@router.get("/rehearsals/{batch_id}/summary", response_model=dict)
def rehearsal_summary(batch_id: int) -> dict:
    """批次汇总：拟保留记录、待复核组、已排除数量，并与采集数据标注待办核对一致性。"""
    summary = rehearsal_service.summary(batch_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"预演批次 {batch_id} 不存在")
    return summary


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集去重比对清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dedup", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按策略编号与状态过滤采集去重比对列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条去重策略明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"去重策略 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条去重策略，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="去重策略已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条去重策略执行提交校验、启用策略、停用策略；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
