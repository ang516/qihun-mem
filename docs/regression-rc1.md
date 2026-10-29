# v1.6-rc4 冻结回归用例索引（regression-rc1）

本文件是 v1.6-rc4 冻结包随附的回归用例索引：P01~P16（解析异常）、C01~C10（公式自检）、
A01~A26（affect / user_affect / 桥接）冻结用例表，外加 v1.6-rc4 新增/追加的
P17 / C11 / A27 / E1~E5 / D01~D12 / X01~X04 / G01~G07 / B01~B07。

## 0. 重建声明与核对口径

- **来源声明**：v1.6-rc1 的原始冻结用例表**未随本仓库提供**（`review/` 只有 rc3 / rc4；
  根目录 `QiHun-Memory-System-v1.5-r2.md` 在 §7.3 处截断，不含 §10/§12 用例清单）。
  本表按 **v1.6-rc4 规范 §12 的类别索引** + **`tests/` 现行事实**重建；「断言要点」是对
  现行测试断言的**转述**，不是 rc1 原文。编号顺序仅作索引，行为判据以 v1.6-rc4 规范正文为准。
- **node 真实性**：表中每个测试 node 都在当前仓库中 grep 校验过对应的 `def test_...` 存在
  （核对日期 2026-10-29）；不存在对应测试的编号按契约要求写「由 v1.5-r2 编号 X 覆盖」并给出真实 node，
  **不编造**。
- **层级**（规范附录 C D4）：L1 解析器单测；L2 状态单测；L3 turn 集成；L4 组合 / 时序 / 迁移 / 降级 / 叙事。
- **最小 CI**：L1 + L2 + L3 + L4 全绿（`python -m pytest -q`）才算冻结通过（§12.8）。
- 用例类别索引（§12.0）：

| 类别 | 编号 | 层 |
|---|---|---|
| 事实层行为 | F01~F15（不在本表逐条展开；由 `tests/test_memory.py`、`test_retrieval.py`、`test_pipeline.py` 覆盖） | L3/L4 |
| 解析异常 | P01~P17 | L1 |
| 公式自检 | C01~C11 | L1 |
| affect/user_affect/桥接 | A01~A27 | L3/L4 |
| 组合/时序/迁移/降级 | X01~X04 | L4 |
| 绕开单点 | G01~G07 | L2/L4 |
| 数据/恢复/容量/对抗 | B01~B07 | L2/L4 |
| 叙事层 | D01~D12 | L3/L4 |

## 1. 解析异常（P01~P16，冻结；P17 为 v1.6 新增）

| 编号 | 断言要点（转述） | 对应测试 node |
|---|---|---|
| P01 | 纯哨兵块：正文为空串，memories 正常解析，raw_len/raw_hash 记录 | `tests/test_parser.py::test_case1_pure_sentinel_block_only` |
| P02 | 哨兵前说明保留为正文（cut 最早哨兵前） | `tests/test_parser.py::test_case2_prefix_explanation_kept_in_answer` |
| P03 | 哨兵后说明丢弃，不进入正文 | `tests/test_parser.py::test_case3_suffix_explanation_discarded` |
| P04 | 哨兵内围栏 JSON 仍可解析 | `tests/test_parser.py::test_case4_fenced_json_inside_sentinel` |
| P05 | 无哨兵：围栏 / 裸 JSON 兜底可解析，正文剔除 JSON 块 | `tests/test_parser.py::test_case5_no_sentinel_fenced_json_fallback`<br>`tests/test_parser.py::test_case5b_no_sentinel_bare_json_fallback` |
| P06 | 字符串外尾逗号可清洗；字符串内 `,}` / `,]` 不被破坏 | `tests/test_parser.py::test_case6_trailing_commas_cleaned_outside_strings`<br>`tests/test_parser.py::test_case6b_trailing_comma_cleanup_keeps_commas_inside_strings` |
| P07 | 截断 / 未闭合 JSON → bad_json + truncated，正文不含残片 | `tests/test_parser.py::test_case7_truncated_json_fragment` |
| P08 | 正文花括号 / 非 memories JSON 不被误判为记忆块 | `tests/test_parser.py::test_case8_braces_in_answer_not_mistaken_for_json`<br>`tests/test_parser.py::test_case8b_non_memories_json_not_cut_from_answer` |
| P09 | 多块 / 多哨兵：JSON 取最后一个可解析块，正文取最早哨兵前 | `tests/test_parser.py::test_case9_multiple_json_blocks_take_last_no_sentinel`<br>`tests/test_parser.py::test_case9b_multiple_sentinel_blocks_take_last` |
| P10 | memories 非数组 / 顶层非对象 / 无哨兵坏候选 → bad_json | `tests/test_parser.py::test_case10_memories_not_list_is_bad_json`<br>`tests/test_parser.py::test_case10b_top_level_not_object_is_bad_json`<br>`tests/test_parser.py::test_case10c_no_sentinel_invalid_candidate_is_bad_json` |
| P11 | 空 memories 数组 → empty，不记 bad_json/fail | `tests/test_parser.py::test_case11_empty_memories_array_is_empty` |
| P12 | 部分条目不合法 → 单条丢弃，保留好条且只含 type/content/tags | `tests/test_parser.py::test_case12_partial_bad_items_keep_good_ones` |
| P13 | content 控制符剥离 / 500 截断 / 含哨兵围栏丢弃 | `tests/test_parser.py::test_content_control_chars_stripped_and_truncated_to_500`<br>`tests/test_parser.py::test_content_with_sentinel_or_fence_dropped` |
| P14 | tags ≤8 且单 tag ≤16，空白项丢弃 | `tests/test_parser.py::test_tags_limited_and_truncated` |
| P15 | 批内按 dedup_key 去重；超过 5 条截断并记 `mem_extract.truncated` | `tests/test_parser.py::test_dedupe_same_type_and_content`<br>`tests/test_parser.py::test_over_five_items_truncated`<br>`tests/test_parser.py::test_exactly_five_items_not_truncated` |
| P16 | 注入防护：`sanitize_for_prompt` 去哨兵 / 围栏 / 伪造块，且幂等 | `tests/test_parser.py::test_case14_sanitize_for_prompt_removes_sentinels_and_fences`<br>`tests/test_parser.py::test_case14b_sanitize_empty_and_defensive`<br>`tests/test_parser.py::test_case14c_fake_sentinel_block_before_real_block_ignored` |
| **P17**（v1.6） | 无哨兵但正文含 `{"memories":[...]}` 片段 → `clean_answer_for_send` 返回去掉 JSON 块后的正文，不整段保留 raw（硬规则 11b） | `tests/test_v16_integration.py::test_p17_no_sentinel_answer_cuts_bracketed_json` |

补充：P05/P08 的发送层边界另由 `tests/test_v15_parser.py::test_clean_answer_for_send_gold` 的金样覆盖；
P13 的 affect 裁剪与 P16 的注入防护在 v1.5 增益后分别还有 `test_normalize_item_*` 系列节点（见 A 类）。

## 2. 公式自检（C01~C10，冻结；C11 为 v1.6 新增）

| 编号 | 断言要点（转述） | 对应测试 node |
|---|---|---|
| C01 | compute_S(init) 基线：valence=0 时 episode=7、fact/preference=30 | `tests/test_v15_state.py::test_compute_S_init_baseline` |
| C02 | init valence 调制：S0·(1+0.5·\|valence\|)，正负对称 | `tests/test_v15_state.py::test_compute_S_init_valence_modulation` |
| C03 | init affect 调制：非 neutral 乘 (1+0.3·intensity)，neutral 不调制 | `tests/test_v15_state.py::test_compute_S_init_affect_modulation` |
| C04 | valence 与 affect 叠乘，cap 90 | `tests/test_v15_state.py::test_compute_S_init_valence_and_affect_multiply`<br>`tests/test_v15_state.py::test_compute_S_init_cap` |
| C05 | revive = 裸 S0，cap 90 | `tests/test_v15_state.py::test_compute_S_revive_is_bare_s0` |
| C06 | reinforce = min(S + S·(1+R), 90)（S=7、R=0.5 → 17.5） | `tests/test_v15_state.py::test_compute_S_reinforce_formula_and_cap` |
| C07 | retention = exp(−dt/S) 参考值 | `tests/test_formulas.py::test_retention_reference_values` |
| C08 | 压抑 S_eff = S·(1−0.2·avoid)，S_eff ≥ 0.8S 且不改写 S | `tests/test_formulas.py::test_suppression_effective_stability` |
| C09 | mood_bias_factor：A 同号 1.0 / 异号 0.0 / 中性 0.5，β=0.2 | `tests/test_formulas.py::test_mood_bias_factor` |
| C10 | 公式单点收口：未知 mode 抛 ValueError、k_v/k_a 可配置、旧薄封装与 compute_S 一致、检索默认值 | `tests/test_v15_state.py::test_compute_S_unknown_mode_raises_value_error`<br>`tests/test_v15_state.py::test_compute_S_kv_and_ka_are_configurable`<br>`tests/test_v15_state.py::test_legacy_wrappers_match_compute_S`<br>`tests/test_formulas.py::test_retrieval_config_defaults` |
| **C11**（v1.6） | `compute_S("init")` 的 \|valence\| 对称（+1.0 与 −1.0 同值）、valence=0 → 7；revive / reinforce 不受 valence 影响 | `tests/test_v16_semantics.py::test_c11_valence_symmetry_and_revive_reinforce_ignore_valence` |

## 3. affect / user_affect / 桥接（A01~A26，冻结；A27 为 v1.6 新增）

| 编号 | 断言要点（转述） | 对应测试 node |
|---|---|---|
| A01 | affect 7 类枚举合法；非枚举 label 只丢字段 | `tests/test_v15_parser.py::test_validate_affect_accepts_all_seven_labels`<br>`tests/test_v15_parser.py::test_affect_case_18_non_enum_labels_are_invalid` |
| A02 | intensity clamp [0,1]、缺省 0、非数字 → None | `tests/test_v15_parser.py::test_validate_affect_out_of_range_intensity_is_clamped`<br>`tests/test_v15_parser.py::test_validate_affect_defaults_intensity_to_zero`<br>`tests/test_v15_parser.py::test_validate_affect_illegal_returns_none` |
| A03 | affect 缺失 / null / neutral 合法但不参与聚合 | `tests/test_v15_parser.py::test_affect_case_20_missing_or_null_affect_means_none`<br>`tests/test_v15_parser.py::test_affect_case_21_neutral_is_legal_but_plain`<br>`tests/test_v15_parser.py::test_validate_affect_missing_or_none_is_none` |
| A04 | normalize_item 保留合法 affect；非法 affect 只丢字段不丢条 | `tests/test_v15_parser.py::test_normalize_item_valid_affect_kept`<br>`tests/test_v15_parser.py::test_normalize_item_invalid_affect_dropped_item_kept_with_metric`<br>`tests/test_v15_parser.py::test_parse_memories_invalid_affect_only_drops_field` |
| A05 | affect 端到端：remember 持久化 affect 并调制 S_init | `tests/test_memory.py::test_remember_persists_affect_and_modulates_s` |
| A06 | 老数据无 affect 照常加载（无 k_a 调制）；旧 trust 字段忽略 | `tests/test_v15_state.py::test_compute_S_old_data_without_affect_has_no_ka`<br>`tests/test_v15_scenarios.py::test_case45_old_data_without_affect_and_trust_field` |
| A07 | 非法 affect 原样搬 `affect.bad.jsonl`，条目仍加载（只丢字段） | `tests/test_v15_capacity.py::test_b1_old_data_migration_tolerates_bad_lines` |
| A08 | user_affect 中性初值 {0.5, 0, 0.3}；未知用户返回初值 | `tests/test_v15_state.py::test_get_unknown_user_returns_config_neutral`<br>`tests/test_v15_state.py::test_neutral_uses_config_initial_values` |
| A09 | step_update 公式 x += Δ·(1−x)，updated_at 刷新 | `tests/test_v15_state.py::test_step_update_formula_and_updated_at` |
| A10 | step_update clamp [0,1]；非法 delta 忽略 | `tests/test_v15_state.py::test_step_update_clamps_unit_interval`<br>`tests/test_v15_state.py::test_step_update_ignores_non_numeric_and_unknown_deltas` |
| A11 | annoyance *= 0.9^floor(days/7)；days 由 updated_at 推导 | `tests/test_v15_state.py::test_decay_annoyance_per_week`<br>`tests/test_v15_state.py::test_decay_days_none_derives_from_updated_at` |
| A12 | stale（≥30 天）→ 整条中性 + familiarity=0，并记 `user_affect.stale` | `tests/test_v15_state.py::test_get_stale_30_days_returns_neutral_and_zero_familiarity`<br>`tests/test_v15_state.py::test_decay_stale_resets_all_and_records_metric` |
| A13 | user_affect 加载容错：未知字段忽略、坏文件中性、越界 clamp | `tests/test_v15_state.py::test_load_ignores_unknown_fields`<br>`tests/test_v15_state.py::test_load_bad_file_is_neutral_with_fail_metric`<br>`tests/test_v15_state.py::test_load_clamps_out_of_range_values` |
| A14 | user_affect 原子写；写失败只记指标不抛 | `tests/test_v15_state.py::test_step_update_persists_atomically`<br>`tests/test_v15_state.py::test_save_failure_records_metric_without_raising` |
| A15 | 9b 聚合：同分量取 max、每轮每分量至多一次 step_update | `tests/test_v15_pipeline.py::test_9b_aggregate_max_within_family`<br>`tests/test_v15_pipeline.py::test_9b_step_update_called_once_per_turn_without_affect` |
| A16 | 9b 只聚合当前用户非 neutral 的 injected_items；桥接条目参与但不跨用户 | `tests/test_pipeline.py::test_9b_aggregates_injected_affect_for_current_user`<br>`tests/test_pipeline.py::test_9b_ignores_other_users_and_neutral`<br>`tests/test_v15_pipeline.py::test_9b_bridge_items_participate_but_only_same_user` |
| A17 | 8a familiarity 每轮最多一次，早于 8b | `tests/test_pipeline.py::test_8a_familiarity_step_once_per_turn`<br>`tests/test_pipeline.py::test_write_back_order_and_single_step_update` |
| A18 | last_event_affect 本轮写入、下一轮检索使用；跨重启失效 | `tests/test_pipeline.py::test_last_event_affect_written_and_used_next_turn`<br>`tests/test_v16_integration.py::test_x02_last_event_affect_lost_after_restart` |
| A19 | affect_match 默认关；开启且 last_event_affect 非空才生效 | `tests/test_retrieval.py::test_affect_match_boost_only_when_enabled_and_event_affect_present` |
| A20 | affect_sim 矩阵与 `retrieval.affect_bias`（只计 rel=1） | `tests/test_retrieval.py::test_affect_similarity_matrix`<br>`tests/test_retrieval.py::test_affect_bias_counts_only_rel_one_items` |
| A21 | 心境一致性 A 与 β 调制；可配置关闭 | `tests/test_retrieval.py::test_mood_bias_same_neutral_opposite_valence`<br>`tests/test_retrieval.py::test_mood_bias_can_be_disabled_by_config` |
| A22 | rel / familiarity(η) / γ_u 只作用于 rel=1，固定数值样例 | `tests/test_retrieval.py::test_rel_user_affect_modulation_applies_only_to_current_user`<br>`tests/test_retrieval.py::test_spec_38_familiarity_and_user_affect_modulation_numbers` |
| A23 | 群聊混合池 rel 标记；dm 池不泄漏他人群聊记忆；scope 隔离 | `tests/test_retrieval.py::test_group_pool_is_mixed_and_rel_marks_current_user_only`<br>`tests/test_retrieval.py::test_dm_pool_does_not_leak_other_users_group_memories_without_trigger`<br>`tests/test_retrieval.py::test_scope_isolation_between_group_and_dm` |
| A24 | 桥接触发：强词单命中 / 弱词≥2 / 弱词+实体 / disabled 安全 | `tests/test_retrieval.py::test_should_bridge_strong_word_single_hit`<br>`tests/test_retrieval.py::test_should_bridge_weak_two_distinct_words_triggers`<br>`tests/test_retrieval.py::test_should_bridge_weak_plus_entity_triggers`<br>`tests/test_retrieval.py::test_should_bridge_disabled_and_bad_input_are_safe`<br>`tests/test_retrieval.py::test_bridge_disabled_returns_dm_pool_only` |
| A25 | 桥接权重 ≤0.7、子预算 ≤300、超限截断 | `tests/test_retrieval.py::test_bridge_item_scored_at_low_weight`<br>`tests/test_retrieval.py::test_bridge_hard_rule_caps_weight_and_budget`<br>`tests/test_retrieval.py::test_bridge_budget_limits_injected_bridge_items`<br>`tests/test_retrieval.py::test_spec_36_bridge_budget_truncates_to_subgroup_limit` |
| A26 | 跨池去重：dedup_key 精确优先、余弦>0.9 二次、保留高分、向量缺失降级 | `tests/test_retrieval.py::test_bridge_cross_pool_dedup_prefers_exact_key`<br>`tests/test_retrieval.py::test_bridge_cross_pool_dedup_uses_cosine_secondary`<br>`tests/test_retrieval.py::test_bridge_cross_pool_dedup_keeps_higher_score_side`<br>`tests/test_retrieval.py::test_bridge_dedup_degrades_to_raw_text_without_vectors`<br>`tests/test_retrieval.py::test_spec_37_cross_pool_dedup_two_pairs` |
| **A27**（v1.6） | 固定数值样例：群聊混合池，同为 fact、R=1、valence=0（A=0.5）；rel=1 sim=0.55/familiarity=1.0/affection=1.0/annoyance=0 vs rel=0 sim=0.85；期望 rel=0 在前（0.51 > 0.42） | `tests/test_v16_integration.py::test_a27_rel0_outscores_rel1_fixed_numeric_sample` |

## 4. v1.6-rc4 新增 / 追加

### 4.0 新增用例（P17 / C11 / A27）

| 编号 | 类别 | 断言要点 | 对应测试 node |
|---|---|---|---|
| P17 | 解析异常（§12.2） | 无哨兵但正文含 `{"memories":[...]}` 片段 → 返回去掉 JSON 块后的正文，不整段保留 raw（硬规则 11b） | `tests/test_v16_integration.py::test_p17_no_sentinel_answer_cuts_bracketed_json` |
| C11 | 公式自检（§12.3） | compute_S(init) 对 \|valence\| 对称、valence=0 → 7；revive/reinforce 不受 valence 影响 | `tests/test_v16_semantics.py::test_c11_valence_symmetry_and_revive_reinforce_ignore_valence` |
| A27 | affect/桥接（§12.4） | 固定数值样例：群聊混合池 fact/R=1/A=0.5，rel=0（0.51）排在 rel=1（0.42）前 | `tests/test_v16_integration.py::test_a27_rel0_outscores_rel1_fixed_numeric_sample` |

> 三条新增用例在 §1 / §2 / §3 的对应表中也同时列出（按类别归档），此处单列以符合 §12.8 冻结包要求。

### 4.1 语义登记（E1~E5，规范 §0.1）

| 编号 | 断言要点 | 对应测试 node |
|---|---|---|
| E1 | 新建条目 `mention_count=0`；仅去重命中 active 时 +1 | `tests/test_v16_semantics.py::test_e1_memory_item_default_mention_count_is_zero`<br>`tests/test_v16_semantics.py::test_e1_remember_new_item_mention_count_is_zero`<br>`tests/test_v16_semantics.py::test_e4_dedupe_hits_active_only_and_just_counts_mention` |
| E2 | 桥接方向口径：仅私聊可桥接群聊记忆，群聊禁止检索任何 dm 记忆（措辞/口径登记，行为沿用 v1.5-r2） | `tests/test_retrieval.py::test_spec_33_group_scope_never_bridges`<br>`tests/test_retrieval.py::test_dm_pool_does_not_leak_other_users_group_memories_without_trigger` |
| E3 | 轻路径只决定是否检索，不决定是否提取；轻路径命中闸门仍附提取协议 | `tests/test_v16_integration.py::test_e3_light_path_still_gated_extraction` |
| E4 | 去重范围 = 同 user + scope + type 的 active；命中只 Set(last_access)+Inc(mention_count)，不强化/不写新行/不进 pending；dormant 不参与；失败记 `mem_write.mention_fail` | `tests/test_v16_semantics.py::test_e4_dedupe_hits_active_only_and_just_counts_mention`<br>`tests/test_v16_semantics.py::test_e4_dedupe_does_not_touch_dormant`<br>`tests/test_v16_semantics.py::test_e4_dedupe_scope_is_same_user_scope_type`<br>`tests/test_v16_semantics.py::test_e4_dedupe_update_failure_records_mention_fail` |
| E5 | dormant 且 sim>0.7 只 revive（不 Inc(access_count)、不叠 reinforce）；`revived_this_turn` 同轮去重；sim=0.5 不强化 | `tests/test_v16_semantics.py::test_e5_dormant_strong_match_revives_without_access_count`<br>`tests/test_v16_semantics.py::test_e5_revived_this_turn_blocks_reinforce`<br>`tests/test_v16_semantics.py::test_e5_reinforce_threshold_is_strictly_greater_than_0_5`<br>`tests/test_v16_semantics.py::test_e5_dormant_mid_similarity_not_revived` |

### 4.2 叙事层（D01~D12，规范 §12.8）

| 编号 | 场景 / 断言要点 | 对应测试 node |
|---|---|---|
| D01 | 日记生成失败：不写日记、记 `diary.fail`，回答正常 | `tests/test_v16_narrative.py::test_d01_diary_generation_failure_degrades` |
| D02 | 私聊不注群日记：只注 dm 日记，group 不出现 | `tests/test_v16_narrative.py::test_d02_dm_does_not_inject_group_diary` |
| D03 | 群聊不注私日记：只注 group 日记，dm 不出现 | `tests/test_v16_narrative.py::test_d03_group_does_not_inject_dm_diary` |
| D04 | 他人昵称脱敏：替换为「有人」并记 `diary.privacy` | `tests/test_v16_narrative.py::test_d04_other_nickname_redacted` |
| D05 | 日记+检索+桥接互不挤占：日记 ≤300、检索（含桥接子预算 ≤300）≤1200、最近轮 ≤750；加 persona/摘要/协议/系统提示后总 ≤3850；超限按「日记→检索→最近轮」截断并记 `prompt.budget_over` | `tests/test_v16_narrative.py::test_d05_diary_retrieval_recent_budgets_coexist`<br>`tests/test_v16_budget.py::test_d05_fixed_components_fit_total_budget` |
| D06 | 幂等：同 `(date, scope_tag, turn_range)` 跳过并记 `diary.dup` | `tests/test_v16_narrative.py::test_d06_diary_idempotent` |
| D07 | 满 30 天移档：`created_at ≤ now-30d` 移 `diary.archive.jsonl`，不再注入 | `tests/test_v16_narrative.py::test_d07_diary_archive_after_30_days` |
| D08 | every_n 按 scope 分别计数：群聊 50 轮只触发 group 日记，私聊计数不受影响 | `tests/test_v16_narrative.py::test_d08_every_n_counts_per_scope` |
| D09 | 隐私护栏三策略：他人昵称→有人、bot 自身昵称→我、user_id 拒写 | `tests/test_v16_narrative.py::test_d09_privacy_three_strategies` |
| D10 | mixed：v1.6 无人工确认入口，只验证不自动生成 / 不落盘 / 不自动注入（临时注入推迟 v1.7） | `tests/test_v16_narrative.py::test_d10_mixed_not_generated_or_injected` |
| D11 | 同 scope 窗口已有 30 篇 active 再触发 daily：不写新日记、记 `diary.cap_hit`，已有 30 篇不被移档 | `tests/test_v16_narrative.py::test_d11_diary_cap_30_per_scope` |
| D12 | 窗口内只有 1 篇 dm、注入请求 2 篇：只注入 1 篇、不回退 archive、`diary.inject=1`、无 error | `tests/test_v16_narrative.py::test_d12_single_dm_diary_injected_without_fallback` |

叙事层单测补充（非 §12.8 编号，随测试文件）：日记生成 / 存储 / 注入边界见
`tests/test_v16_diary.py`；prompt 预算单测见 `tests/test_v16_budget.py`。

### 4.3 组合 / 时序 / 迁移 / 降级（X01~X04）

| 编号 | 断言要点 | 对应测试 node |
|---|---|---|
| X01 | 桥接 + 去重 + 截断组合：桥接与 dm 重复跨池去重、桥接子预算截断共存且指标齐全 | **由 v1.5-r2 编号 43 覆盖**：`tests/test_v15_scenarios.py::test_case43_dedup_and_bridge_truncation_coexist`<br>`tests/test_retrieval.py::test_spec_43_dedup_and_bridge_truncation_coexist`（v1.6 未新增独立 X01 节点） |
| X02 | last_event_affect 跨轮 + 重启：重启后为空，affect_match 自动失效、不报错 | `tests/test_v16_integration.py::test_x02_last_event_affect_lost_after_restart` |
| X03 | 迁移：老数据（无 affect / 无 diary / 旧 trust 字段）升级后正常加载与检索 | **由 v1.5-r2 编号 45 覆盖**：`tests/test_v15_scenarios.py::test_case45_old_data_without_affect_and_trust_field`<br>坏行迁移容忍：`tests/test_v15_capacity.py::test_b1_old_data_migration_tolerates_bad_lines` |
| X04 | 降级链：embedding 不可用时 retrieval.degraded / dedup.degraded 可见，回答照常 | `tests/test_v16_integration.py::test_x04_embedding_degraded_chain_end_to_end` |

### 4.4 绕开单点（G01~G07）

| 编号 | 断言要点 | 对应测试 node |
|---|---|---|
| G01 | 直接写 memories 状态字段被架构测试拦截（只能经 `update_memory`） | `tests/test_v15_guards.py::test_a1_state_field_writes_only_inside_update_memory` |
| G02 | 非法 patch：白名单外字段 / 非法值整体不生效 | `tests/test_v15_guards.py::test_a1_update_memory_whitelist_fields`<br>`tests/test_v15_guards.py::test_a1_public_state_mutations_go_through_update_memory` |
| G03 | Inc 顺序 / 原子性：同批 Inc 读前序结果；单写者重放不重复计数 | `tests/test_memory.py::test_update_memory_inc_reads_old_value`<br>`tests/test_v15_state.py::test_update_memory_same_batch_inc_reads_prior_result` |
| G04 | 直接写 `user_affect.json` 被拦截（字面量 / 文件 IO 只在 `UserAffectStore`） | `tests/test_v15_guards.py::test_a4_user_affect_json_literal_only_in_config`<br>`tests/test_v15_guards.py::test_a4_no_direct_file_io_referencing_user_affect` |
| G05 | 非法 mode：`compute_S` 抛 `ValueError`，不静默降级 | `tests/test_v15_guards.py::test_a5_invalid_mode_raises_value_error` |
| G06 | 发送层碰 memories：`clean_answer_for_send` 只接收单个 raw_answer，不接触 memories | `tests/test_v15_guards.py::test_a6_clean_answer_for_send_never_touches_memories` |
| G07 | fill 硬编码 bridge：装填预算全部跟随 constraints，不硬编码 1200/300 | `tests/test_v15_guards.py::test_a7_fill_does_not_hardcode_total_or_head`<br>`tests/test_v15_guards.py::test_a7_bridge_budget_follows_constraints_not_hardcoded_300` |

### 4.5 数据 / 恢复 / 容量 / 对抗（B01~B07）

| 编号 | 断言要点 | 对应测试 node |
|---|---|---|
| B01 | 迁移 / 老数据：坏行隔离，不中断加载 | `tests/test_v15_capacity.py::test_b1_old_data_migration_tolerates_bad_lines` |
| B02 | pending 重放：只重放新记忆、turn_key 幂等；状态更新不进 pending | `tests/test_v15_capacity.py::test_b2_pending_replay_only_new_memory_and_turn_key_idempotent`<br>`tests/test_v15_capacity.py::test_b2_state_updates_never_enter_pending` |
| B03 | 半写 / 崩溃：残留 *.tmp 与截断末行可恢复 | `tests/test_v15_capacity.py::test_b3_tmp_leftover_and_truncated_last_line` |
| B04 | 跨文件一致性：affect 写失败时回答与记忆不受影响 | `tests/test_v15_capacity.py::test_b4_affect_write_failure_keeps_answer_and_memory` |
| B05 | 容量边界：检索时延与 token 预算、桥接候选翻倍与子预算 | `tests/test_v15_capacity.py::test_b5_retrieval_latency_and_token_budget`<br>`tests/test_v15_capacity.py::test_b5_bridge_candidates_double_and_sub_budget` |
| B06 | 对抗输入：端到端不崩、不外泄 | `tests/test_v15_capacity.py::test_b6_adversarial_inputs_end_to_end` |
| B07 | 降级链：无异常、指标可见 | `tests/test_v15_capacity.py::test_b7_degradation_chain_no_exception` |

### 4.6 接口与架构守卫（D7 边界 / service / CLI，v1.6 追加）

规范附录 C D7 给出叙事层五条边界；v1.6-rc4 用架构守卫测试守护，另新增 service / CLI 接口用例。
本组编号不在 §12.8 的 D01~D12 范围内，单列以便逐条追溯。

| 编号 | 断言要点 | 对应测试 node |
|---|---|---|
| D7.1 | 叙事层不参与检索打分、装填、S/R、去重键、user_affect 聚合；diary 不 import 检索/单点 | `tests/test_v16_guards.py::test_d71_retrieval_and_memory_do_not_import_diary_for_scoring`<br>`tests/test_v16_guards.py::test_d71_diary_does_not_import_retrieval_scoring_or_single_points` |
| D7.1 | diary 落盘只经 `DiaryStore`（唯一读写入口） | `tests/test_v16_guards.py::test_d71_diary_store_is_only_written_by_diary_module` |
| D7.2 | 日记注入只读 `ctx.diary_excerpts`；write_back 不得修改日记 | `tests/test_v16_guards.py::test_d72_write_back_does_not_modify_diary`<br>`tests/test_v16_guards.py::test_d72_ctx_diary_excerpts_are_frozen` |
| D7.3 | 日记生成走独立 worker，turn() 内不调用 generate | `tests/test_v16_guards.py::test_d73_turn_never_calls_diary_generation` |
| D7.4/D7.5 | mixed 不进入正常生命周期（不落盘、不自动注入）；日记失败不阻塞回答 | `tests/test_v16_guards.py::test_d75_mixed_never_persisted_or_injected`<br>`tests/test_v16_guards.py::test_diary_failure_never_breaks_answer` |
| 接口 | `/diary/*` 两阶段 + `/health` 日记计数；错误码 400/404/503；CLI `diary list\|prompt\|add` | `tests/test_v16_service_cli.py::test_diary_http_two_phase_and_health`<br>`tests/test_v16_service_cli.py::test_diary_http_errors`<br>`tests/test_v16_service_cli.py::test_cli_diary_list_prompt_add` |

## 5. 编号到 node 的核对方式

```bash
# 逐条核对：本文件所有测试 node 均可在仓库内 grep 到对应的 def test_...
python -m pytest tests/test_parser.py tests/test_v15_parser.py tests/test_formulas.py \
  tests/test_v15_state.py tests/test_v15_pipeline.py tests/test_pipeline.py tests/test_memory.py \
  tests/test_retrieval.py tests/test_v16_semantics.py tests/test_v16_narrative.py \
  tests/test_v16_budget.py tests/test_v16_integration.py tests/test_v15_scenarios.py \
  tests/test_v15_capacity.py tests/test_v15_guards.py -q
python -m pytest -q        # 最小 CI：L1~L4 全绿
```

已知无法一一对应的编号与处理：

- **X01**：v1.6-rc4 未新增独立 X01 测试节点，组合断言沿用 v1.5-r2 用例 43 的两个真实 node（见上表）。
- **X03**：迁移断言沿用 v1.5-r2 用例 45 与 B01 的真实 node（v1.6 未新增独立迁移测试）。
- **E2**：桥接方向为措辞/口径登记，行为未变，沿用 v1.5-r2 的 scope/桥接节点。
- **F01~F15**：事实层行为类别（§12.1），v1.6-rc4 未给出逐条编号内容，本表不虚构；覆盖点见
  `tests/test_memory.py`、`tests/test_retrieval.py`、`tests/test_pipeline.py`、`tests/test_v15_scenarios.py`。
