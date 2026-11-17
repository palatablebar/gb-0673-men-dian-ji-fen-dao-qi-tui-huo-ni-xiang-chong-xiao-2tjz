# 门店积分到期与退货逆向冲销

给连锁零售会员运营专员使用。消费先用最早到期批次；退款按原消费分配逆向恢复，已过期部分不得复活。

七个业务模块：事件采信、权益规则、积分归批、消费到期、退货回拨、余额对账、期末归档；另有账户合并入口，处理主账户与待合并账户的归属裁决。

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
```

访问 http://localhost:18242 。启动直接载入现场脏批。切换干净样例和旧编码样例可对照核验。会员00001两批积分100和80，消费120后退款30，其中20恢复、10已过期不恢复；再消费50后余额30。整批发放1580、余额1010、消费730、退款170、到期10，守恒可复核。

各模块页面对应 `/<模块英文名>`，各模块执行入口为 `POST /api/<模块英文名>`，不传参即可独立演示。账户合并页 `/merge`，裁决入口 `POST /api/merge`，确认生效 `POST /api/merges/{merge_id}/settle`，切换失败回退 `/fail`，按最新依据重算 `/retry`，历史列表 `GET /api/merges`。完整运行 `POST /api/runs` 支持 `dataset`（dirty/clean/legacy）、`csv_text` 与 `overrides` 参数。批次历史 `GET /api/runs`，详情 `GET /api/runs/{run_id}`。

同输入与规则版本生成相同批号，只保留一条批次。复核入口 `POST /api/runs/{run_id}/review` 提交当前 `revision`，陈旧提交409。批次、合并的确认同样按 `revision` 防陈旧。归档包只有一个下载入口 `GET /api/export/{run_id}`，含证据和运行摘要。原始隔离在页面同屏显示。

SQLite使用命名卷，容器重启后保留历史。要重建演示可在Compose中改用新的卷名称，保留旧卷。合并记录与批次同库持久化，重建批次不影响既有合并记录的状态与依据。

源码在 `app/`，样例在 `sample_data/`，测试在 `tests/`；定义见 `docs/DATA-DICTIONARY.md`，模块和边界测试对应关系见 `docs/DEPTH.md`。输入上限2MB，属于离线复核工具。

采信页支持上传或粘贴CSV。完整源字节与参数随批次持久化，跨页通过run_id读取同一结果。`POST /api/runs/{run_id}/rerun` 读取该批不可变输入快照重新计算，规则未变时批号不变。切换另一示例则创建或复用另一批次。
