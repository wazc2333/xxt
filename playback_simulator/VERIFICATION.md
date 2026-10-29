# 验证记录

日期：2026-10-24；环境：Windows，Python 3.13.14。

执行 `python -m unittest -v test_simulator`：14 项测试全部通过，耗时 4.501 秒。

实际 1 倍速演示：

```powershell
python simulator.py --duration 6 --interval 1 --pause-at 2 --pause-for 1 --fail-first --output verified-demo
```

结果：`completed=true`，`position=6`，`watched=6`，`retries=1`，`progressChecks=9`。事件记录显示 2 秒处暂停和恢复，暂停前后确认位置均为 2 秒。重试的序号 1 未重复累计进度。

跨进程恢复演示：

```powershell
python simulator.py --duration 4 --interval 1 --stop-at 2 --output verified-resume
python simulator.py --duration 4 --interval 1 --resume --output verified-resume
```

第一次确认停在 2 秒，`active=false`、`completed=false`；第二次从 2 秒恢复，最终 `position=4`、`watched=4`、`completed=true`。

运行结果保存在上述两个输出目录。所有网络验证仅使用 `127.0.0.1` 本机模拟服务。这些验证不涉及真实站点接收时长，也不验证真实服务端的完成规则。

## 真实服务器适配器新增验证

执行 `python -m unittest -v`：共 28 项测试全部通过，耗时 4.626 秒。原有 14 项本机测试保留，另外 14 项验证真实接口适配器。真实适配器测试使用内存拦截，不启动 mock HTTP server，不向真实服务器发请求。

关键验证包括：真实 GET host/路径与扁平 query；MD5 输入和整数位置；`otherInfo` 的嵌入 `courseId`；Cookie 文件格式及域限制；媒体令牌刷新；真实时间推进；响应 `isPassed` 与未知累计秒数的区分；跨进程恢复；服务端限制停止；进度请求不盲目重试。

此外已用当前保存的真实卡片/媒体数据做离线比较：除运行时 `_t` 外，17 个构造参数与已有请求契约一致；真实 query 保留额外 `courseId` 字段。`python simulator.py` 输出的预览目标为 `mooc1.chaoxing.com`，时长 1093 秒、配置间隔 60 秒。该预览未加载 Cookie、未联网。

此结果证明请求构造和客户端状态处理通过离线验证，不能证明真实服务器接受了上报或累计了学习时长。

## 默认真实发送行为更新

按用户后续要求，CLI 现在默认真实发送，显式 `--dry-run` 才离线预览，`--send` 保留兼容。上文不带参数的预览命令属于旧版本验证记录；当前不可用该命令进行离线测试。

新增 4 项 CLI 拦截测试后，`python -m unittest -v` 共 32 项全部通过，耗时 4.590 秒。所有实际上传函数在这些测试中均被拦截或替换为本机测试服务，未实际发送视频进度。当前离线预览命令为 `python simulator.py --dry-run`。

另有用户明确授权的作业暂存/提交真实验证，与视频进度区分，见 `D:\xxt\analysis\assessment\作业业务协议.md`。
