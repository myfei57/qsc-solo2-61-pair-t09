# 多泵排水调度（tunnelnet.dispatch）

从“一坑一泵、启停随缘”改造为**按水位与泵健康排序、按累计运行时长轮换**的多泵共享调度。
无第三方依赖，复用项目的 `Store`（原子 JSON 落盘）与审计体系。

## 1. 关键决策（题目要求二选一的部分）

| 议题 | 选定策略 | 理由 |
|---|---|---|
| 高水位抢排：全部一起上 vs 逐台加泵 | **逐台加泵（staggered start）** | 电机启动浪涌电流是额定值的约 1.8 倍；同时合闸会叠加浪涌，供电紧张时直接跳闸，反而一台都排不了。调度器每个错峰窗口（`stagger_seconds`，默认 10s）**全系统只放一台启动**，每次单独校验功率预算。 |
| 保护期结束：立即参与 vs 排到下一轮 | **排到下一轮** | 候选集合在**计划生成时刻快照冻结**。保护期内的泵根本不进入计划；计划执行期间保护期到点的泵不会被动态插入当前计划，只进入之后新生成的计划。语义简单、可审计、不会在执行中途改变步序。 |

其余规则：

* **目标台数（迟滞）**：每个坑配置上升带 `start_levels` 与一一对应的下降带 `stop_levels`。
  水位越过第 i 个启动带增加第 i 台泵；水位必须**低于**第 i 个停止带才摘掉第 i 台泵。
  到达 `high_level` 时，目标台数 = 当前可用泵总数（抢排）。
* **健康**：`healthy > degraded > failed`。`failed` 泵被隔离，永不入选；
  同健康等级按**累计运行时长升序**（短的先上），编号兜底保证次序确定。
* **轮换**：请求轮换时，把当前累计时长长的在役泵换下场，换上累计时长短的健康空闲泵；
  顺序是**先启新泵、确认后再停旧泵**（`after_seq` 联动），坑内不会瞬间失去排水能力。
  可设 `rotation_gap_seconds`，时长差不足时不轮换。
* **保护期（防频繁启停）**：每台泵配置 `cooldown_seconds`，自上次停机起算，保护期内不可被选中。
* **水落停机**：优先停累计时长长的，强制停机不受“最短运行时间”约束。

## 2. 多坑一致性与供电

* 所有请求经**同一把进程内 RLock 串行化**：多坑同时上报不会把同一台泵租给两个坑。
* 泵是**独占租约**资源：运行会话 + 未完成计划步共同构成“忙泵集合”，选泵时排除。
* 全系统一个功率预算 `watts_capacity`：每次启动前按
  `当前运行功率 + 候选泵浪涌功率 ≤ 容量` 校验，跨坑共享。
* 多个坑排队等待启动名额时，按 `severity = 水位 + 高水位加成(10)` 排序，
  越危险的坑先获得下一个启动窗口。
* 若在役基础功率 + 最小浪涌**永远**超预算，计划终态为 `blocked`（释放未启动步，保留在役泵）；
  若只是暂时超（如别的坑泵稍后会停），步保持 `pending`，后续 `tick()` 自动继续。

## 3. 部分失败与幂等

* **启动失败是部分失败**：主选启动失败 → 泵置 `failed` 健康状态隔离；
  计划在生成时为每个缺口冻结了**同数量的后备泵（reserve）**，按冻结次序自动顶上。
  后备顶上后达到目标台数，计划仍记 `completed`；后备耗尽仍达不到目标才是 `partial`。
  后备顶替不额外消耗错峰窗口（是替换而非新增浪涌），但仍过功率校验。
* **请求幂等**：`request_id` 相同的重复上报直接返回同一计划，不产生第二次硬件命令、
  不重复发审计（记 `request.duplicate`）。
* **命令幂等**：会话在硬件命令**之前**落盘（`starting/stopping` 中间态）。
  崩溃重启后调用 `reconcile()`：以泵车实际状态裁决中间态（已转就确认、没转就按失败处理），
  再继续执行，重复 `tick()` 安全。
* **停机失败**：不封泵，步回 `pending`，下个 tick / 人工处理后重试。

## 4. 落盘、审计与“同一口径”

`Store` 目录下的持久化种类：

```
dispatch-pump/<id>     泵档案（额定/浪涌功率、保护期、服务坑集合、健康）
dispatch-pit/<id>      坑的水位带配置
dispatch-plan/<id>     调度计划 = 落盘的调度结果（有序 start/stop 步及状态机）
dispatch-session/<id>  运行段台账（每次连续运行一条，停机时写秒数与 Wh）
dispatch-state/<key>   全局错峰窗口游标、计划序号
audit/<seq>-<event>    哈希链审计（防篡改、可校验）
```

**运行时长只有一份口径**：`run_session` 台账。

* 泵档案里看到的累计时长、轮换/补泵的排序依据、用电统计
  全部经过 `ledger.cumulative_seconds()` / `ledger.energy_wh()` 即时计算；
  未关闭的运行段按当前时刻推算。不存在第二份计数器，因此不会“对不上账”。
* 审计为追加写、SHA-256 哈希链：`EventLog.verify_chain()` 可检测删除、篡改、断序。
* 事件类型：`pump.registered`、`pit.registered`、`plan.built`、`pump.started`、
  `pump.start-failed`、`step.promoted`、`pump.stopped`、`pump.stop-failed`、
  `plan.finished`、`plan.blocked`、`request.duplicate`、`reconcile`、`pump.health`。

## 5. 计划状态机

计划 `executing` → `completed`（在役台数达标）/ `partial`（尽力后仍不达标）/
`blocked`（功率容量物理上无法再加泵）/ `superseded`（被同坑更新的计划取代；
已在跑的泵保留，由新计划负责停）。

步状态：`pending → starting → started → stopping → done`，
冻结后备 `reserve`（提升后变 `pending`），
失败 `failed`、到点不可用 `skipped`、未用后备/被取代 `superseded`。

## 6. 用法

```python
svc = DispatchService(store, rig, watts_capacity=4000, stagger_seconds=10,
                      clock=clock, audit=EventLog(store))
svc.register_pump(PumpProfile("P1", watts=1000, serves=("A", "B"), cooldown_seconds=300))
svc.register_pit(PitConfig("A", start_levels=(0.4, 0.6, 0.8),
                           stop_levels=(0.2, 0.3, 0.5), high_level=0.9))

svc.report_level("A", 0.95, request_id="<dedupe-key>")  # 高水位 -> 逐台加泵
svc.tick()                                             # 周期性推进错峰/重试
svc.request_rotation("A", request_id="...")            # 时长均衡轮换
svc.reconcile()                                        # 崩溃恢复
svc.pump_statistics()                                  # 档案视图（台账口径）
pump_power_report(store, now)                          # 用电统计（同一台账口径）
svc.audit_verify()                                     # 审计链校验
```

`rig` 是 `PumpRig` 协议（`start/stop/is_running`），生产环境接 PLC/IO 适配，
测试与演示用内置的 `SimRig`。可运行演示：

```bash
python -m tunnelnet.dispatch.demo
```

对应行为测试见 `tests/test_dispatch.py`（迟滞带、健康优先、保护期下轮、
逐台错峰、多坑不双租、功率阻断、部分失败后备、幂等、崩溃恢复、台账一致、审计防篡改）。
