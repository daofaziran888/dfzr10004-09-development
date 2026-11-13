"""采集去重比对业务规则：状态流转、字段校验与筛选口径都收在这里。

去重预演也收在本模块：按指定时间窗口与比对字段计算重复组，按排序字段与保留策略
给出拟保留记录，并为被排除记录给出裁决依据。人工复核裁定拥有最高优先级：高风险或
相似度落在边界区间的组会写入采集数据标注的待复核清单，复核结论回写同一去重批次，
后续预演不得用策略计算结果覆盖人工结论。
"""
from __future__ import annotations

import threading
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

from app.store import store

MODULE = "dedup"
RECORD_MODULE = "dedup_record"
REHEARSAL_MODULE = "dedup_rehearsal"
GROUP_MODULE = "dedup_group"
DECISION_MODULE = "dedup_decision"
TAG_MODULE = "tag"

REQUIRED_FIELDS = ["策略编号", "业务键列", "排序字段"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用策略": "已启用", "停用策略": "已停用"}
NEGATIVE_ACTIONS = ["停用策略"]

DEFAULT_THRESHOLD = 0.85
BOUNDARY_DELTA = 0.05  # 相似度落在阈值 ±0.05 的组视为边界组，进入人工复核
TAG_REVIEW_TYPE = "去重复核"
REVIEW_PENDING = "待复核"

# 重复组的「查重-建组」临界区：两个批次并发提交同一业务键时，先到者建组生效，
# 后到者复用既有组，保证同一业务键在同一时间窗口内只生成一个去重组。
_GROUP_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def _parse_threshold(raw: Any) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD
    return value if 0 < value <= 1 else DEFAULT_THRESHOLD


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _ordered_members(members: list[dict[str, Any]], keep_strategy: str) -> list[dict[str, Any]]:
    """按排序字段与保留策略排队；排序值相同（相似度相同）时保留先到达的记录。"""
    if keep_strategy == "保留最早":
        return sorted(members, key=lambda m: (str(m.get("排序值", "")), int(m.get("到达序号", 0))))
    return sorted(members, key=lambda m: (str(m.get("排序值", "")), -int(m.get("到达序号", 0))), reverse=True)


def _cluster_records(records: list[dict[str, Any]], threshold: float) -> list[list[dict[str, Any]]]:
    """并查集聚类：业务键相同或比对内容相似度达到阈值的记录归入同一重复组。"""
    parent = list(range(len(records)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            first, second = records[i], records[j]
            same_key = first.get("业务键") == second.get("业务键")
            if same_key or _similarity(str(first.get("比对内容", "")), str(second.get("比对内容", ""))) >= threshold:
                union(i, j)

    clusters: dict[int, list[dict[str, Any]]] = {}
    for index, record in enumerate(records):
        clusters.setdefault(find(index), []).append(record)
    return sorted(clusters.values(), key=lambda members: min(int(m.get("到达序号", 0)) for m in members))


class DedupService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"去重策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集去重比对可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"去重策略已{action}"


class DedupRehearsalService:
    """去重预演：计算重复组、给出拟保留与排除依据、承接人工复核裁定。"""

    # ---------- 预演 ----------

    def run_rehearsal(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        strategy_no = str(values.get("策略编号") or "").strip()
        strategy = next((row for row in store.rows(MODULE) if row.get("策略编号") == strategy_no), None)
        if strategy is None:
            return None, f"去重策略 {strategy_no or '（未填写）'} 不存在，请先在采集去重比对中登记策略"
        window_start = str(values.get("窗口开始") or strategy.get("时间窗口") or "").strip()
        window_end = str(values.get("窗口结束") or window_start).strip()
        if not window_start:
            return None, "策略未配置时间窗口，本次也未指定窗口，无法圈定比对范围"
        compare_field = str(values.get("比对字段") or strategy.get("比对字段") or "").strip()
        threshold = _parse_threshold(strategy.get("相似阈值"))
        candidates = [
            record for record in store.rows(RECORD_MODULE)
            if window_start <= str(record.get("采集时间", ""))[:10] <= window_end
        ]

        with _GROUP_LOCK:
            batch = self._new_batch(strategy, window_start, window_end, compare_field, threshold, len(candidates))
            if not candidates:
                batch["空窗口"] = True
                store.rows(REHEARSAL_MODULE).append(batch)
                return batch, f"时间窗口 {window_start} ~ {window_end} 内没有可比记录，未生成重复组"

            created: list[dict[str, Any]] = []
            reused: list[dict[str, Any]] = []
            for members in _cluster_records(candidates, threshold):
                if len(members) < 2:
                    continue
                key_text = "＋".join(sorted({str(m.get("业务键", "")) for m in members}))
                existing = self._find_group(strategy_no, window_start, window_end, key_text)
                if existing is not None:
                    reused.append(existing)
                    continue
                group = self._build_group(
                    batch=batch, members=members, strategy=strategy,
                    threshold=threshold, key_text=key_text,
                    window_start=window_start, window_end=window_end,
                )
                store.rows(GROUP_MODULE).append(group)
                created.append(group)
                if group["复核状态"] == REVIEW_PENDING:
                    self._upsert_tag_todo(batch, group)

            batch["组编号列表"] = [group["组编号"] for group in created + reused]
            self._refresh_batch(batch)
            store.rows(REHEARSAL_MODULE).append(batch)

        parts = [f"预演完成：窗口 {window_start} ~ {window_end} 内可比记录 {len(candidates)} 条"]
        parts.append(f"计算重复组 {len(created) + len(reused)} 个（新建 {len(created)}，复用既有 {len(reused)}）")
        if reused:
            nos = "、".join(group["组编号"] for group in reused)
            parts.append(f"同一业务键的回执再次送达只生成一个去重组：{nos} 已由先到批次生效，本次合并到既有组")
        if batch["待复核组数"]:
            parts.append(f"{batch['待复核组数']} 个高风险或边界相似组已进入采集数据标注待复核清单")
        return batch, "；".join(parts)

    def list_rehearsals(self, *, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        rows = sorted(store.rows(REHEARSAL_MODULE), key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_rehearsal(self, batch_id: int) -> dict[str, Any] | None:
        batch = store.find(REHEARSAL_MODULE, batch_id)
        if batch is None:
            return None
        return {"batch": batch, "groups": self._batch_groups(batch)}

    # ---------- 人工裁决 ----------

    def adjudicate(
        self,
        batch_id: int,
        decisions: list[dict[str, Any]],
        reviewer: str,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        batch = store.find(REHEARSAL_MODULE, batch_id)
        if batch is None:
            return None, []
        groups_by_no = {group["组编号"]: group for group in self._batch_groups(batch)}
        results: list[dict[str, Any]] = []
        with _GROUP_LOCK:
            for decision in decisions:
                group_no = str(decision.get("组编号") or "").strip()
                group = groups_by_no.get(group_no)
                if group is None:
                    results.append({"组编号": group_no, "ok": False, "message": "重复组不属于本批次，未处理"})
                    continue
                if group["回执状态"] == "已回执":
                    results.append({
                        "组编号": group_no,
                        "ok": True,
                        "message": f"业务键 {group['业务键']} 的去重组已回执生效，重复送达不再生成新组",
                    })
                    continue
                keep = str(decision.get("保留记录") or group["拟保留记录"]).strip()
                member_ids = [member["记录编号"] for member in group["成员"]]
                if keep not in member_ids:
                    results.append({"组编号": group_no, "ok": False, "message": f"保留记录 {keep or '（空）'} 不在组内，未处理"})
                    continue
                whole_group = bool(decision.get("整组确认"))
                if keep != group["拟保留记录"]:
                    self._apply_manual_keep(group, keep, reviewer)
                group["复核状态"] = "已复核"
                group["回执状态"] = "已回执"
                group["pending"] = False
                group["裁决人"] = reviewer
                group["裁决时间"] = _now()
                self._record_decision(group, keep, reviewer, batch)
                self._close_tag_todo(group, keep, reviewer)
                action_text = "整组回执" if whole_group else "逐条裁决"
                results.append({"组编号": group_no, "ok": True, "message": f"{action_text}完成，保留 {keep}"})
            self._refresh_batch(batch)
        return batch, results

    def summary(self, batch_id: int) -> dict[str, Any] | None:
        batch = store.find(REHEARSAL_MODULE, batch_id)
        if batch is None:
            return None
        groups = self._batch_groups(batch)
        kept = [group["拟保留记录"] for group in groups]
        pending_groups = [group for group in groups if group["复核状态"] == REVIEW_PENDING]
        excluded_total = sum(1 for group in groups for member in group["成员"] if member["角色"] == "排除")
        tag_rows = [row for row in store.rows(TAG_MODULE) if row.get("关联预演") == batch["预演编号"]]
        tag_todos = [row for row in tag_rows if str(row.get("复核状态", "")).startswith("待")]

        details = []
        consistent = True
        for group in groups:
            excluded = [member for member in group["成员"] if member["角色"] == "排除"]
            linked = [row for row in tag_rows if row.get("关联组") == group["组编号"]]
            if group["复核状态"] == REVIEW_PENDING:
                ok = (
                    len(linked) == 1
                    and all(str(row.get("复核状态", "")).startswith("待") for row in linked)
                    and all(
                        row.get("拟保留记录") == group["拟保留记录"]
                        and int(row.get("排除数量", -1)) == len(excluded)
                        for row in linked
                    )
                )
            else:
                ok = all(not str(row.get("复核状态", "")).startswith("待") for row in linked)
            consistent = consistent and ok
            details.append({
                "组编号": group["组编号"],
                "业务键": group["业务键"],
                "拟保留记录": group["拟保留记录"],
                "排除数量": len(excluded),
                "复核状态": group["复核状态"],
                "标注记录": linked[0]["标注编号"] if linked else None,
                "与标注待办一致": ok,
            })
        overall = consistent and len(pending_groups) == len(tag_todos)
        return {
            "预演编号": batch["预演编号"],
            "拟保留记录数": len(kept),
            "拟保留记录": kept,
            "待复核组数": len(pending_groups),
            "已排除数量": excluded_total,
            "标注待办数": len(tag_todos),
            "一致性": overall,
            "一致性说明": "拟保留记录、待复核组与已排除数量同采集数据标注待办一致"
            if overall else "去重结论与采集数据标注待办存在偏差，请核对",
            "明细": details,
        }

    # ---------- 内部步骤 ----------

    def _new_batch(
        self,
        strategy: dict[str, Any],
        window_start: str,
        window_end: str,
        compare_field: str,
        threshold: float,
        candidate_count: int,
    ) -> dict[str, Any]:
        rows = store.rows(REHEARSAL_MODULE)
        batch_id = _next_id(rows)
        return {
            "id": batch_id,
            "status": "预演完成",
            "pending": False,
            "abnormal": False,
            "预演编号": f"DRY-{batch_id:04d}",
            "策略编号": strategy.get("策略编号"),
            "窗口开始": window_start,
            "窗口结束": window_end,
            "比对字段": compare_field,
            "相似阈值": threshold,
            "排序字段": strategy.get("排序字段"),
            "保留策略": strategy.get("保留策略") or "保留最新",
            "可比记录数": candidate_count,
            "拟保留数": 0,
            "待复核组数": 0,
            "已排除数": 0,
            "空窗口": False,
            "组编号列表": [],
            "创建时间": _now(),
        }

    def _find_group(self, strategy_no: str, window_start: str, window_end: str, key_text: str) -> dict[str, Any] | None:
        for group in store.rows(GROUP_MODULE):
            if (
                group.get("策略编号") == strategy_no
                and group.get("窗口开始") == window_start
                and group.get("窗口结束") == window_end
                and group.get("业务键") == key_text
            ):
                return group
        return None

    def _build_group(
        self,
        *,
        batch: dict[str, Any],
        members: list[dict[str, Any]],
        strategy: dict[str, Any],
        threshold: float,
        key_text: str,
        window_start: str,
        window_end: str,
    ) -> dict[str, Any]:
        sort_field = str(strategy.get("排序字段") or "排序值")
        keep_strategy = str(strategy.get("保留策略") or "保留最新")
        ordered = _ordered_members(members, keep_strategy)
        manual = self._manual_decision(str(strategy.get("策略编号")), key_text, ordered)
        keep_id = manual["保留记录"] if manual else str(ordered[0]["记录编号"])
        keep_record = next(member for member in ordered if str(member["记录编号"]) == keep_id)

        member_rows = []
        similarities = []
        for member in ordered:
            record_no = str(member["记录编号"])
            if record_no == keep_id:
                basis = "人工裁定保留" if manual else f"按排序字段「{sort_field}」{keep_strategy}，本记录排序值最优"
                member_rows.append(self._member_row(member, "拟保留", basis))
                continue
            sim = _similarity(str(member.get("比对内容", "")), str(keep_record.get("比对内容", "")))
            similarities.append(sim)
            basis = self._exclude_basis(
                member, keep_record, sim=sim, threshold=threshold,
                sort_field=sort_field, keep_strategy=keep_strategy, manual=manual,
            )
            member_rows.append(self._member_row(member, "排除", basis))

        group_sim = min(similarities) if similarities else 1.0
        high_risk = any(str(member.get("风险等级")) == "高" for member in ordered)
        boundary = abs(group_sim - threshold) <= BOUNDARY_DELTA
        risk_mark = "高风险" if high_risk else ("边界相似" if boundary else "常规")
        if manual is not None:
            review_status = "已复核"
        elif high_risk or boundary:
            review_status = REVIEW_PENDING
        else:
            review_status = "无需复核"

        rows = store.rows(GROUP_MODULE)
        group_id = _next_id(rows)
        return {
            "id": group_id,
            "status": "生效中",
            "pending": review_status == REVIEW_PENDING,
            "abnormal": high_risk,
            "组编号": f"GRP-{group_id:04d}",
            "预演编号": batch["预演编号"],
            "策略编号": strategy.get("策略编号"),
            "窗口开始": window_start,
            "窗口结束": window_end,
            "业务键": key_text,
            "相似度": round(group_sim, 4),
            "风险标记": risk_mark,
            "拟保留记录": keep_id,
            "复核状态": review_status,
            "回执状态": "未回执",
            "人工裁定": manual is not None,
            "裁决人": manual.get("裁决人") if manual else None,
            "裁决时间": manual.get("裁决时间") if manual else None,
            "成员": member_rows,
            "创建时间": _now(),
        }

    def _member_row(self, record: dict[str, Any], role: str, basis: str) -> dict[str, Any]:
        return {
            "记录编号": record.get("记录编号"),
            "业务键": record.get("业务键"),
            "比对内容": record.get("比对内容"),
            "排序值": record.get("排序值"),
            "采集时间": record.get("采集时间"),
            "风险等级": record.get("风险等级"),
            "到达序号": record.get("到达序号"),
            "角色": role,
            "裁决依据": basis,
        }

    def _exclude_basis(
        self,
        member: dict[str, Any],
        keep_record: dict[str, Any],
        *,
        sim: float,
        threshold: float,
        sort_field: str,
        keep_strategy: str,
        manual: dict[str, Any] | None,
    ) -> str:
        if manual is not None:
            return (
                f"人工裁定优先：{manual['裁决人']} 于 {manual['裁决时间']} 复核裁定保留 "
                f"{manual['保留记录']}，策略计算结果不再覆盖，本记录予以排除"
            )
        parts = []
        if member.get("业务键") == keep_record.get("业务键"):
            parts.append(f"业务键相同（{member.get('业务键')}）")
        if sim >= threshold:
            parts.append(f"相似度 {sim:.2f} ≥ 阈值 {threshold:.2f}")
        if not parts:
            parts.append(f"经组内传递比对归入本重复组（与拟保留记录相似度 {sim:.2f}）")
        if str(member.get("排序值", "")) == str(keep_record.get("排序值", "")):
            parts.append("排序值相同，按到达顺序保留先到的记录")
        else:
            parts.append(
                f"按排序字段「{sort_field}」{keep_strategy}，拟保留记录排序值 "
                f"{keep_record.get('排序值')} 优先于本记录 {member.get('排序值')}"
            )
        return "；".join(parts) + "，予以排除"

    def _manual_decision(
        self,
        strategy_no: str,
        key_text: str,
        members: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        keys = set(key_text.split("＋"))
        member_ids = {str(member.get("记录编号")) for member in members}
        decisions = [
            row for row in store.rows(DECISION_MODULE)
            if row.get("策略编号") == strategy_no
            and row.get("业务键") in keys
            and str(row.get("保留记录")) in member_ids
        ]
        if not decisions:
            return None
        return max(decisions, key=lambda row: int(row.get("id", 0)))

    def _record_decision(self, group: dict[str, Any], keep: str, reviewer: str, batch: dict[str, Any]) -> None:
        rows = store.rows(DECISION_MODULE)
        rows.append({
            "id": _next_id(rows),
            "status": "已生效",
            "pending": False,
            "abnormal": False,
            "策略编号": group["策略编号"],
            "业务键": group["业务键"],
            "保留记录": keep,
            "裁决人": reviewer,
            "裁决时间": _now(),
            "来源预演": batch["预演编号"],
            "来源组": group["组编号"],
        })

    def _apply_manual_keep(self, group: dict[str, Any], keep: str, reviewer: str) -> None:
        decided_at = _now()
        for member in group["成员"]:
            if member["记录编号"] == keep:
                member["角色"] = "拟保留"
                member["裁决依据"] = "人工裁定保留"
            else:
                member["角色"] = "排除"
                member["裁决依据"] = (
                    f"人工裁定优先：{reviewer} 于 {decided_at} 复核裁定保留 {keep}，"
                    "策略计算结果不再覆盖，本记录予以排除"
                )
        group["拟保留记录"] = keep
        group["人工裁定"] = True

    # ---------- 标注待办联动 ----------

    def _upsert_tag_todo(self, batch: dict[str, Any], group: dict[str, Any]) -> None:
        rows = store.rows(TAG_MODULE)
        excluded_count = sum(1 for member in group["成员"] if member["角色"] == "排除")
        linked = next((row for row in rows if row.get("关联组") == group["组编号"]), None)
        if linked is None:
            linked = next(
                (
                    row for row in rows
                    if row.get("标注类型") == TAG_REVIEW_TYPE
                    and str(row.get("复核状态", "")).startswith("待")
                    and row.get("数据记录") == group["业务键"]
                    and not row.get("关联组")
                ),
                None,
            )
        values = {
            "标注批次": batch["预演编号"],
            "标注标签": group["风险标记"],
            "复核状态": REVIEW_PENDING,
            "关联预演": batch["预演编号"],
            "关联组": group["组编号"],
            "拟保留记录": group["拟保留记录"],
            "排除数量": excluded_count,
        }
        if linked is not None:
            linked.update(values)
            linked["status"] = "已标注"
            linked["pending"] = True
            return
        tag_id = _next_id(rows)
        rows.append({
            "id": tag_id,
            "status": "已标注",
            "pending": True,
            "abnormal": group["风险标记"] == "高风险",
            "标注编号": f"TAG-{tag_id:04d}",
            "数据记录": group["业务键"],
            "标注类型": TAG_REVIEW_TYPE,
            "标注人": "系统",
            "标注时间": _now()[:10],
            **values,
        })

    def _close_tag_todo(self, group: dict[str, Any], keep: str, reviewer: str) -> None:
        for row in store.rows(TAG_MODULE):
            if row.get("关联组") == group["组编号"] and str(row.get("复核状态", "")).startswith("待"):
                row["复核状态"] = f"已复核：保留 {keep}"
                row["status"] = "已复核"
                row["pending"] = False
                row["复核人"] = reviewer
                row["拟保留记录"] = keep
                row["排除数量"] = sum(1 for member in group["成员"] if member["角色"] == "排除")

    # ---------- 汇总 ----------

    def _batch_groups(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        rows = store.rows(GROUP_MODULE)
        return [group for no in batch.get("组编号列表", []) for group in rows if group["组编号"] == no]

    def _refresh_batch(self, batch: dict[str, Any]) -> None:
        groups = self._batch_groups(batch)
        batch["拟保留数"] = len(groups)
        batch["已排除数"] = sum(1 for group in groups for member in group["成员"] if member["角色"] == "排除")
        batch["待复核组数"] = sum(1 for group in groups if group["复核状态"] == REVIEW_PENDING)
        batch["pending"] = batch["待复核组数"] > 0
        if batch["待复核组数"]:
            batch["status"] = "待复核"
        elif groups and all(group["回执状态"] == "已回执" for group in groups):
            batch["status"] = "已闭环"
        else:
            batch["status"] = "预演完成"
