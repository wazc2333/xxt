# 课程学习台

章节和作业的“全选”仅选择服务器已确认未完成的项目；作业还须处于可提交状态。状态未知时可手动选择。作业列表同时显示完成状态，点击“刷新作业”更新，不需要打开答题页。

Python 后端与同源前端，可部署在 Windows、Ubuntu；使用 cryptography 加密本地登录凭证。用户可选择学习通扫码登录或 Cookie 登录，自动获取“我学的课”，进入课程页面后只展示章节标题，可多选章节一键学习。程序按目录顺序执行，到达当前章节才读取其中的任务；视频按实际时间运行播放事件与进度请求，章节测验按界面策略处理。

交付包为 `dist/xxt-backend.zip`，附 SHA-256 校验文件。可通过 `python deploy/build_release.py` 重新构建，仅打包部署需要的文件。

默认对接超星真实接口。点击“一键学习所选章节”后会实际发送进度请求；默认勾选的“自动提交章节测验”也会正式提交支持的测验。章节测验默认选择题查本地题库，未命中调用 DeepSeek，简答题直接生成，与作业共用当前用户的 SK、题库、模型和思考配置。支持的内嵌加密字体先在本地还原为正文。勾选“随机选择选项”后绕过题库和 DeepSeek，保留已有有效作答，空白选择题随机选一个实际选项。生成或随机答案均不保证正确或通过。不支持的题型或无法确认的字形等显示具体原因并跳过。程序不解码视频，不执行独立考试。视频完成与服务器累计学时分开显示，后者未在接口返回时保持未知。测验具体支持范围和协议依据见 [backend/CHAPTER_QUIZ.md](backend/CHAPTER_QUIZ.md)。

## Windows 启动

需要 Python 3.11 或更高版本。在项目根目录安装用于本地凭证加密的依赖，然后启动：

```powershell
python -m pip install -r requirements.txt
python -m backend.server
# 或
powershell -ExecutionPolicy Bypass -File .\deploy\start-windows.ps1
```

浏览器访问 `http://127.0.0.1:8080`。默认显示“扫码登录”，使用学习通 App 扫描二维码并在手机确认；也可切换到“Cookie 登录”粘贴有效 Cookie。登录后进入课程，勾选多个章节并点击“一键学习所选章节”。可取消勾选“自动提交章节测验”以跳过测验。关闭浏览器不会停止服务内的任务；再次打开页面可查看当前会话的任务。使用 Ctrl+C 正常停止服务。

扫码登录使用超星官方登录页下发的二维码及状态接口，显示等待扫码、等待手机确认、取消和过期状态，过期后点击“刷新二维码”。本地二维码有效期 150 秒，最多每 3 秒向官方轮询一次；切换到 Cookie 登录会取消当前扫码会话。二维码及待确认扫码凭证仅在内存中。确认后转为与 Cookie 登录相同的服务会话并获取课程，成功登录的上游 Cookie 加密保存用于恢复。需要额外验证的账号请在官方网页完成验证后使用 Cookie 登录。

选课后仅读取章节目录，不打开首章，也不预读任何章节任务。课程页显示章节标题、选择框及服务器目录中的完成状态：绿色“✓ 已完成”、待完成任务点数量，或“状态未知”；目录提供总数时显示课程任务点完成进度。支持多选、全选，切换课程保留本页会话中的选择。点击“重读目录”更新标题和完成状态，“刷新”课程列表会清除目录缓存。会话内的平台页面请求共用至少 0.5 秒的请求发起间隔。

完成标记是最近一次读取服务器目录的结果，与章节选择、任务详情加载状态和本地执行进度独立。不会根据本地任务结束直接显示绿色勾；执行后可点击“重读目录”查看平台最新确认。目录 API 的 `catalog.chapters[]` 返回 `isCompleted`（布尔或 null）、`remainingTasks`（待完成任务点数量或 null）及 `completionSource`（已识别时为 `server_directory`）；`status` 仍表示任务详情加载状态。`catalog.taskProgress` 为服务器课程任务点的 `{completed,total}`，未提供时为 null。不从待完成数量推算章节任务总数。

启动前重新核对目录中的所选章节，若章节已变化则停止，不会回退为整门课程执行。随后逐章读取任务并顺序处理；当前章节结束前不预读下一章。已完成的视频、测验跳过。文档或未知类型标记需要处理，支持的其他任务继续执行。可随时请求停止；当前请求返回后结束，尚未到达的章节不读取任务。

测验“提交被接受”与“任务点通过”分别计数。响应被接受后重新读取卡片确认通过；随机选择未通过时不会反复重答。提交前创建不含凭证或答案的持久记录，网络结果不明时即使重启也不会自动重复提交。进度条表示已处理章节比例，100% 不代表所有任务均通过；部分完成、需要处理和服务器确认分别显示。

平台若重定向到访问验证页，会明确提示“平台要求访问验证”并停止本会话后续读取，不再误报课程格式变化。请在超星原网页手动完成验证，再更新 Cookie 登录。程序不自动处理验证码；请求节流不能保证平台始终不要求验证。

本机更新部署可执行 `deploy/restart-windows.ps1`：仅替换当前项目占用指定端口的后端，以隐藏窗口启动，并检查健康接口的版本与源码一致。更新前会检查加密依赖。重启恢复尚未过期的用户登录，不自动恢复运行任务；管理员仍需重新登录。

## Ubuntu 启动

适用于提供 Python 3.11+ 的系统，例如 Ubuntu 24.04 自带的 Python 3.12：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv ca-certificates
cd /path/to/xxt
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
bash deploy/start-ubuntu.sh
```

本地访问 `http://127.0.0.1:8080`。部署到服务器后，可先通过 SSH 转发访问：

```bash
ssh -L 8080:127.0.0.1:8080 user@your-server
```

然后在自己电脑打开 `http://127.0.0.1:8080`。所有路径使用 pathlib，后端不依赖 Windows 命令。

## Ubuntu 常驻服务与 HTTPS

把本项目部署包解压到 `/opt/xxt`，不要复制个人的 `cookie.txt`、`analysis/private` 或旧抓包。创建服务账号和数据目录：

```bash
sudo useradd --system --home /opt/xxt --shell /usr/sbin/nologin xxt
sudo install -d -o xxt -g xxt -m 700 /var/lib/xxt
sudo python3 -m venv /opt/xxt/.venv
sudo /opt/xxt/.venv/bin/python -m pip install -r /opt/xxt/requirements.txt
sudo cp deploy/xxt.service /etc/systemd/system/xxt.service
```

将 service 中 `https://learn.example.com` 改成实际访问来源，再执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now xxt
sudo systemctl status xxt
```

后端监听本机，由 nginx 提供 HTTPS。`deploy/nginx.conf` 提供模板，替换域名、已有证书路径后加载并用 `nginx -t` 检查。`--public-origin` 必须与浏览器访问的协议、域名、端口完全一致，不带结尾斜杠；反向代理保留 Host、Origin 和 Cookie。不要在子路径下挂载。HTTPS 模式会设置 Secure 会话 Cookie。对外传输登录凭证时使用 HTTPS。

采用单进程部署；运行任务保存在进程内存，用户登录由同一数据目录恢复，禁止多个后端同时共享该目录。若经共享反向代理访问，登录限流按代理 IP 计算（每分钟最多 10 次），可在代理配置独立限流后按部署需要调整。

## Docker（Windows Docker Desktop / Ubuntu）

```bash
docker compose up -d --build
```

访问 `http://localhost:8080`。容器以非 root 运行，命名卷保存任务记录、登录历史及加密登录凭证。`.dockerignore` 使用白名单，个人 Cookie 文件和分析抓包不会进入构建上下文。通过 HTTPS 反向代理发布时设置来源：

```bash
PUBLIC_ORIGIN=https://learn.example.com docker compose up -d --build
```

PowerShell 中先 `$env:PUBLIC_ORIGIN='https://learn.example.com'`，再运行 `docker compose up -d --build`。

## 作业列表与一键提交

进入课程后切换“作业”，展示作业标题和状态。在章节或作业页下方填写当前用户通用的 DeepSeek SK（可保存，也可填写后直接启动），任一入口修改或清除后所有场景同步生效。选择作业并点击“一键提交所选作业”。程序按列表顺序逐份获取详情；选择题默认查询本地题库，未命中调用 DeepSeek，简答题直接生成且不查询题库。支持已识别的单选、多选、判断和简答控件；未知题型或答案不完整时不提交该份作业。生成答案不保证正确。

使用 `deepseek-flash`，当前对应 V4.1-Flash-1017，思考 `low`。SK 随当前登录凭证加密保存，重启可恢复；不会返回前端或展示给管理员。题干和选项会发送给 DeepSeek，API 调用产生服务商用量；不会向 DeepSeek 发送超星 Cookie、课程签名或作答表单。

本地题库提供 JSON 导入、导出和格式示例，按当前登录会话隔离并保存在 `data/question-bank.sqlite3`。选择题生成结果会缓存并标记来源；简答题每次生成。关闭浏览器与后端重启保留题库；退出后重新登录可通过导出/导入迁移题库。任务进度显示题库命中、DeepSeek 生成、接受提交与列表确认数量。验证码或 API 凭证/额度/网络问题停止相关流程，提交结果不明不会自动重交。

完整参数、题库格式和支持范围见 [backend/HOMEWORK.md](backend/HOMEWORK.md)。本次实现与测试没有使用真实 SK 调用模型或向真实平台提交作业。

## 运行方式与限制

- Cookie 支持原始 Cookie 请求头、浏览器导出的 JSON、Netscape 格式。每个浏览器会话独立，默认 30 天未访问后过期；访问时续期。浏览器以持久化 HttpOnly Cookie 保存本服务随机会话标识，不把上游 Cookie 放入 localStorage。关闭同一浏览器再打开、重启后端后可恢复。需要使用同一个访问主机名；`localhost` 与 `127.0.0.1`、不同浏览器或隐私窗口不会共享登录。
- 上游 Cookie、服务会话和课程上下文经 Fernet 认证加密后保存到数据目录 `saved-sessions.sqlite3`，密钥为 `session-encryption.key`；两者仅限服务账号/部署者访问。管理员列表与登录历史不包含密钥或凭证。主动退出/过期删除可恢复登录，保留登录历史；数据目录、密钥和密文都不进入发行包。备份与迁移必须保留原密钥及数据库，密钥缺失时拒绝生成替代密钥。管理员登录仍只保存在内存，后端重启后需重新认证。
- 关闭/重启后端会中断任务，恢复登录不会自动续跑任务。再次启动时会重新读取服务器状态、跳过 `isPassed=true` 的视频；未完成的视频从头运行，不把离线时间计入进度。磁盘检查点保留用于核查，不自动恢复不确定的上报。登录恢复使用已加密的课程缓存，不自动向上游发请求；官方登录提前失效、要求验证或清除浏览器数据时仍需重新登录。旧版尚未持久化的登录需首次重新登录。
- 每个会话同时运行一个后台任务（读取目录或学习），默认全局最多 4 个，可通过 `--max-jobs` 调整。前端每次选一门课程、最多 1000 个章节；同一课程可分批运行。使用卡片下发的真实上报间隔，不提供倍速推进。前端始终显式传入所选 `chapterIds`；API 保留旧版 `taskIds` 视频选择方式和省略两者时的按课程视频执行方式。
- 同一会话缓存最近 10 门课程的目录，支持最多 2000 个可识别章节。旧版全任务扫描最多读取每门课程 5000 项任务。刷新课程清除缓存；目录重读失败保留上次成功目录。遇到平台访问验证码时停止后续平台请求并提示手动处理。
- 获取课程使用当前已验证的普通“我学的课”根目录模板。支持该模板下的多章节、多卡片、多视频；特殊班课、文件夹、其他模板尚未验证，缺少所需结构时会停止并说明。
- “所选章节处理完成”仅指所选章节内已识别的任务处理结果，不能代表整门课程完成。遇到不支持内容、测验跳过或服务器尚未确认通过时显示“部分完成”。
- 验证码、人脸识别、互动验证、特定插件、部分片段视频和服务器限制不自动处理。请求失败或结果不确定时停止，进度写入请求不自动重试；不会凭本地 100% 判断服务器已完成。
- 任务停止可能等待当前请求返回（单次超时 20 秒）；在停止位置发送暂停事件，保存最后一次有效响应。若服务器未返回确定结果，检查点保留 pending 标记。
- 会话按浏览器 Cookie 隔离；不同浏览器账户无法读取对方任务。API 同源来源校验、CSRF 令牌、固定上游域名和请求体大小限制均已实现。服务不会读取部署机器上的个人 Cookie 文件。

## 管理后台与登录历史

访问 `http://127.0.0.1:8080/admin`，使用独立管理员账号登录。管理员可查看姓名/昵称、超星 UID、扫码或 Cookie 登录方式、登录时间、最近活跃时间、退出时间、会话状态，以及当前会话的课程数量与任务状态。支持搜索、状态/方式筛选、每页 25 条和每 10 秒自动刷新。普通用户的登录会话不能访问管理接口。

点击记录右侧“管理”可为有效登录用户设置或清除通用 DeepSeek SK，只写入新值，不返回或回填当前 SK。选择该用户的课程和任务类型，读取章节或作业列表后选择项目启动；全选仅包含已确认未完成且可执行的项目。可停止当前任务，等待当前请求结束后按新的选择重新启动。任务、模型与正式提交仍使用该用户的登录、题库和 SK，原有防重复提交记录继续有效。运行任务时先停止，再修改 SK。

“删除用户并退出登录”会显示删除范围并确认：已获取 UID 时删除相同 UID 的所有登录记录，撤销全部关联有效会话，清除保存凭证、SK 和各登录的题库；UID 未知时只删除当前记录。已退出的历史记录也可删除。删除后旧会话不能在重启后恢复，管理员自己的登录不受影响。提交防重复记录保留；已经发出的网络请求无法撤回，停止后不再发起后续任务。

首次部署时，在后端使用的同一个数据目录初始化管理员，随后重启后端：

```powershell
python -m backend.admin init --data-dir D:\xxt\data --generate
```

Ubuntu 在 `/opt/xxt` 使用 `.venv/bin/python -m backend.admin init --data-dir /var/lib/xxt --generate`，以运行后端的服务账号执行；Docker 可执行 `docker compose exec backend python -m backend.admin init --data-dir /data --generate`，然后重启该服务。也可省略 `--generate`，通过终端交互输入自定密码，至少 12 个字符。命令拒绝覆盖已有管理员配置。

生成的随机密码仅写入数据目录的 `admin-initial-password.txt`，用户名默认 `admin`；认证配置 `admin-auth.json` 仅保存随机盐和 PBKDF2 密码摘要。初始密码核对并妥善保存后可自行移除该明文文件。管理员空闲 30 分钟或登录满 8 小时需重新登录；每个来源每分钟最多尝试 5 次。初始化不是网页注册，未配置时管理接口拒绝登录。

登录成功后，在数据目录 `user-records.sqlite3` 保存必要的账号展示信息与每次登录记录，退出、过期和重启后继续保留。有效的记住登录在重启后恢复原登录记录；不能恢复的旧内存会话标记为“服务中断”。关闭页面不会自动退出，因此“已登录”只表示会话有效。最近活跃表示本服务收到用户请求的时间；异常停止时最多落后约 15 秒。管理员查看列表不会延长用户会话。历史任务明细不在登录记录中留存；退出后的任务栏显示为空。相同 UID 的多次登录分别记录，账号统计按 UID 去重，未获取 UID 的记录分别计数。

姓名来自官方扫码响应或个人主页，UID 优先读取已登录 Cookie 中的账号标识，仅用于展示和统计，不用于授予权限。读取不到信息时显示占位文字。历史从新版本启用后的成功登录开始积累，无法补回旧版本的内存会话。扫码等待、取消及失败不计为成功登录。

上游 Cookie、二维码凭证、用户/管理员会话令牌及 CSRF 令牌不写入登录历史，管理列表也不返回这些字段。可恢复用户会话的凭证仅保存在单独的加密存储。数据目录同时含账号历史、登录密钥和本机管理员配置，应仅授权部署管理者与服务账号访问；不要公开静态托管、提交版本库或分享。打包脚本不包含此目录。前端登录页说明了管理员可见信息及历史保留行为。

## API

所有写入使用 `Content-Type: application/json`、同源 `Origin`、`X-Requested-With: learning-web`。用户登录、扫码登录和管理员登录接口之外的写入另需相应会话的 `X-CSRF-Token`，令牌由登录/会话接口返回；浏览器服务会话采用 HttpOnly Cookie。扫码过程另有独立的 HttpOnly、SameSite=Strict 临时 Cookie 绑定浏览器，其他浏览器不能查询或接管该二维码。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/health` | 服务状态及启动时的源码版本 `version` |
| `POST /api/admin/login` | `{"username":"admin","password":"…"}`，独立管理员认证，设置管理员 Cookie 并返回管理员 CSRF |
| `GET /api/admin/session` | 恢复管理员会话；需要管理员 Cookie |
| `GET /api/admin/users` | 管理员查询登录记录，支持 `search`、`status`、`method`、`page`；返回筛选总数、分页记录和全局统计 |
| `GET /api/admin/users/{recordId}/control` | 用户管理数据；可传 `courseId`、`kind=chapters/homework` 读取已缓存目录；不返回 SK、Cookie 或签名地址 |
| `POST /api/admin/users/{recordId}/sk` | `{"key":"新值"}`，更新该登录用户通用 SK；空字符串清除 |
| `POST /api/admin/users/{recordId}/scan` | `{"courseId":"…","kind":"chapters"}`，或 `homework`，只读取对应列表 |
| `POST /api/admin/users/{recordId}/start` | 章节传 `courseId`、`kind=chapters`、明确的 `chapterIds`、`quizPolicy`；作业传 `kind=homework` 和 `workIds` |
| `POST /api/admin/users/{recordId}/stop` | `{"jobId":"…"}`，只停止该用户的指定任务 |
| `POST /api/admin/users/{recordId}/delete` | `{}`，删除账号关联记录、退出登录并清理凭证和题库；上述写入均需管理员 CSRF |
| `POST /api/admin/logout` | `{}`，退出管理员会话；需要管理员 CSRF |
| `POST /api/login` | `{"cookie":"…"}`，验证并返回课程、CSRF 令牌 |
| `POST /api/login/qr` | `{}`，创建当前浏览器的扫码会话，返回图片 data URI、状态和剩余有效期；刷新时替换旧二维码 |
| `POST /api/login/qr/status` | `{}`，查询本浏览器扫码状态；确认成功后设置服务会话 Cookie，返回课程和 CSRF 令牌 |
| `POST /api/login/qr/cancel` | `{}`，取消扫码并清除临时 Cookie |
| `GET /api/session` | 恢复网页会话、课程、最近任务 |
| `GET /api/courses` | 只读刷新课程（运行任务时拒绝） |
| `GET /api/courses/{id}/homework` | 返回本会话缓存的作业标题、选择标识及可选状态，不返回平台签名链接 |
| `POST /api/courses/{id}/homework/scan` | `{}`，启动只读取作业列表的任务，不打开详情 |
| `GET /api/ai/settings` | 用户通用配置：SK 是否已配置、模型及题库条数，不返回 SK；原 `/api/homework/settings` 保留为兼容别名 |
| `POST /api/ai/settings` | `{"key":"…"}`，加密保存通用 SK；空字符串清除；兼容原作业配置入口 |
| `POST /api/homework/bank/import` | `{"questions":[…]}`，为当前登录导入本地选择题题库 |
| `GET /api/homework/bank` | 导出当前登录的本地题库 |
| `POST /api/homework/jobs` | `{"courseId":"…","workIds":["…"]}`，顺序处理明确选择的作业 |
| `POST /api/courses/{id}/tasks/scan` | 前端使用 `{"scope":"outline"}` 仅读取章节目录；兼容 `initial`、`chapters` 及 `{}` 全任务扫描 |
| `GET /api/courses/{id}/tasks` | 返回 `ready`、按章节分组所需的 `tasks` 数据和不可读章节说明 |
| `POST /api/jobs` | `{"courseIds":["课程 ID"],"chapterIds":["章节 ID"],"quizPolicy":"bank_submit"}`；`quizPolicy` 为 `skip`（API 默认）、`bank_submit`（前端默认，题库→DeepSeek）或 `random_submit`（随机选项）。不能同时提供 `taskIds` |
| `GET /api/jobs/{id}` | 查询本会话任务进度 |
| `POST /api/jobs/{id}/stop` | `{}`，停止任务 |
| `POST /api/logout` | `{}`，退出并停止本会话任务 |

任务状态包括 `queued/running/completed/partial/failed/stopped`。`kind=catalog` 表示读取目录或旧版任务列表；`kind=learning` 表示当前章节执行方式，`kind=playback` 为旧版视频方式。`selectedChapterIds/selectedChapterCount`、`chapterQueue`、`processedChapters/completedChapters` 记录章节选择与进度。`submittedQuizzes`、`passedQuizzes`、`alreadyPassedQuizzes`、`unconfirmedQuizzes` 分别记录本次接受提交、确认通过、原已通过和未确认通过的测验。`passedVideos/alreadyPassedVideos` 为视频确认数量；`localPosition` 是本地位置，`lastResponsePosition` 是最后有效响应位置。`serverCreditedSeconds` 保持 `null`。目录只返回展示信息，不返回卡片凭证或上报 URL。

`kind=homework_catalog` 表示只读取作业标题，`kind=homework` 为作业处理队列；`homeworkQueue`、`selectedHomeworkCount/processedHomework`、`submittedHomework/confirmedHomework/alreadySubmittedHomework/unconfirmedHomework` 以及 `bankHits/deepseekQuestions/shortAnswersGenerated` 分别记录队列、提交确认和答案来源数量，不返回答案或 SK。

目录返回 `chapters` 及各章 `pending/loaded/unavailable` 状态。`catalogScope` 表明本次扫描范围；任务进度的章节计数仅针对本次范围。需要用户手动完成平台访问验证时，任务返回 `actionRequired=platform_verification`，同时保留上次缓存。

## 验证

```bash
python -m unittest discover -s backend/tests -v
python -m unittest discover -s playback_simulator -v
```

自动化单元测试通过受控平台适配器和时钟运行，HTTP 测试仅访问回环服务，不发送真实学习进度。另行授权的真实服务端测试与只读验证记录见 `backend/VERIFICATION.md`。Windows 启动脚本、Ubuntu systemd、nginx、Docker 是部署入口，Ubuntu/Docker 运行结果需在目标环境进一步验证。

本项目不包含远程关机接口。之前请求的本机关机是一次性操作，与后端服务无关。
