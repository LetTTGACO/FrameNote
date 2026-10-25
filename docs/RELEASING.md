# 发布版本、升级和回滚

每次部署绑定一个版本、源码提交和独立环境。回滚使用保留的上一版环境；任务数据、配置和模型按各自兼容性处理。

## 版本规则

版本对应一次正式交付，日常提交不逐次升版本。准备发布时，查看上个发布标签到当前提交的最终差异，汇总用户可见变化，选择版本并更新唯一版本来源和 `CHANGELOG.md`。测试、审查及最终安装包隔离验证通过后，再合并并推送主分支与标签。

| 改动 | 处理方式 |
| --- | --- |
| 错字、排版、补充已有用法等纯说明文档修改 | 正常提交，不单独发版，不改变已发布包 |
| 修正已有行为、兼容性、安全或运行依赖问题 | 递增修订号，例如 `0.2.0` → `0.2.1` |
| 新增 CLI / HTTP / MCP 功能或用户可用的执行工作流 | 递增次版本、修订号归零，例如 `0.2.1` → `0.3.0` |
| 不兼容的接口、配置或材料变化 | 当前 `0.x` 阶段递增次版本，明确迁移和回滚限制；进入 `1.x` 后递增主版本 |
| 提供候选包验收 | 使用 Python 包版本格式，例如 `0.3.0rc1`，标签为 `v0.3.0rc1` |

判断依据是实际行为，而不是文件扩展名。`SKILL.md` 只调整措辞属于纯说明；修正已有 Agent 执行错误按修复发布，新增执行能力按功能发布。客户端、配置和随源码归档交付的 Skill 都属于项目交付范围，即使服务端代码没有改变。

文档可以继续在 `main` 更新。需要再次交付包含修改的完整发行包时，使用新版本；不能从最新 `main` 重建并覆盖同名旧包，也不移动已发布标签。重新部署既有版本时，使用原发布包及其校验值。

客户端与服务端可分别更新：只修改客户端或 Skill 时，依据接口兼容性决定是否更新服务。`/api/health` 报告的是运行中的服务版本，不表示本机客户端已同步；客户端 / Skill 的发布来源由对应源码归档版本和校验值确定。

v0.2.1 是现有通用工作流与客户端保护的修订发布，沿用 v0.2.0 的接口和材料格式。软件版本号与材料格式号分别维护，回滚仍按下文核对目标版本的实际兼容范围。

## 识别运行版本

版本唯一来源为 `framenote/__init__.py` 的 `__version__`，打包元数据从这里读取。查询本机安装或运行中的服务：

```bash
python -m framenote.version
curl --fail http://127.0.0.1:18121/api/health
```

两者包含 `version`、完整 `commit` 和 `dirty`。源码检出显示当前提交及改动状态；安装包显示构建时内嵌的身份。无法确定提交时返回 `null`，不使用猜测值。`dirty=false` 且提交明确的构建才可归档成正式发布包。`/api/ready` 继续负责 ASR 就绪检查。

## 制作发布包

1. 修改唯一版本来源，更新 `CHANGELOG.md`，完成测试、类型检查、发行检查和审查后提交。
2. 从该干净提交构建一次 wheel 与源码归档：

   ```bash
   python -m build
   ```

3. 在目标平台的最终版本目录创建环境并安装这份 wheel。例如 Ubuntu：

   ```bash
   python3.10 -m venv /opt/framenote/releases/v0.2.1/.venv
   /opt/framenote/releases/v0.2.1/.venv/bin/python -m pip install \
     '/path/to/framenote-0.2.1-py3-none-any.whl[asr]'
   ```

   按服务用户执行并事先准备目录权限。在最终绝对路径创建环境，后续只切换 `current` 指针，不移动虚拟环境。Python 虚拟环境中的脚本依赖原路径，参见 [Python 文档](https://docs.python.org/3.12/library/venv.html#how-venvs-work)。

4. 切换到源码目录之外，使用刚安装的环境归档。将输入改为同一次构建的实际路径：

   ```bash
   cd /
   /opt/framenote/releases/v0.2.1/.venv/bin/python -m framenote.release archive \
     --wheel /path/to/framenote-0.2.1-py3-none-any.whl \
     --sdist /path/to/framenote-0.2.1.tar.gz \
     --output /opt/framenote/artifacts/v0.2.1
   ```

   工具先执行 `pip check`，核对安装环境、wheel 和源码归档的构建身份一致，再保存：

   | 文件 | 内容 |
   | --- | --- |
   | wheel、源码归档 | 本次已验证构建的原始字节 |
   | `release.json` | 版本、目标标签、提交、平台与 Python、依赖清单、材料格式范围、文件哈希 |
   | `requirements.lock` | 当前环境中实际安装的包名与精确版本 |
   | `models.lock.json` | 该版本的模型资产锁 |
   | `SHA256SUMS` | 包含 `release.json` 在内的文件完整性校验 |

   输出目录已经存在时拒绝覆盖。归档不包含配置、Cookie、workspace 或浏览器会话，不读取环境变量或依赖下载 URL。校验值用于完整性检查，可信来源仍由发布和传输过程保证。

5. 核验归档：

   ```bash
   /opt/framenote/releases/v0.2.1/.venv/bin/python -m framenote.release verify \
     /opt/framenote/artifacts/v0.2.1
   ```

6. 候选环境验收通过后，为同一个构建提交创建标签：

   ```bash
   git tag -a v0.2.1 -m 'FrameNote v0.2.1' VERIFIED_COMMIT
   ```

   `VERIFIED_COMMIT` 必须等于发布包中的完整提交号。标签发布后不移动；后续修复使用新版本。构建、打标签、推送发布包和生产部署是可分别完成的步骤。

依赖清单记录精确版本，不包含第三方 wheel 字节。即时回滚依靠保留的旧虚拟环境；需要离线重建时，还应在对应平台缓存清单里的 wheel：

```bash
/opt/framenote/releases/v0.2.1/.venv/bin/python -m pip wheel \
  --no-deps --find-links /opt/framenote/artifacts/v0.2.1 \
  --wheel-dir /opt/framenote/wheelhouse/v0.2.1 \
  -r /opt/framenote/artifacts/v0.2.1/requirements.lock
```

wheel 缓存按平台和 Python 环境准备；只有源码分发的依赖会在此时构建 wheel，单独保存缓存校验值。系统 Python、ffmpeg、libsndfile 和 JavaScript 运行时的版本写入部署记录；应用目录切换不会恢复这些系统依赖。

## 部署目录与候选验证

| 路径 | 用途 |
| --- | --- |
| `/opt/framenote/releases/v0.2.1/.venv` | 本版独立环境，保留原绝对路径 |
| `/opt/framenote/artifacts/v0.2.1` | 本版发布包与清单 |
| `/opt/framenote/current` | 指向当前版本目录的符号链接 |
| `/etc/framenote/service.env` | 正式配置；备份与目标版本配套核对 |
| `/var/lib/framenote` | 正式任务数据，独立于应用环境 |
| `/opt/framenote/models` | 已核验模型；改变模型锁时另建模型目录，保留旧文件 |

候选实例在独立 workspace 和端口运行，使用同一份已构建 wheel。依次检查依赖、模型哈希、运行版本、`/api/ready=200`、旧材料读取和短视频 / 截图流程。正式 workspace 不用于候选试跑。

`FRAMENOTE_WORKSPACE` 与模型路径应使用绝对路径。默认 `FRAMENOTE_ASR_PYTHON` 跟随当前服务解释器；若显式配置该值，升级和回滚时也须切到匹配环境，避免服务与推理运行在不同版本。

## 升级切换

候选验收完成后，安排一次短暂停服：

1. 停止提交新任务，记录旧版本真实目录、版本 / 提交、配置、unit、依赖与模型信息。旧的 `0.1.0` 实例必须连同提交或安装包保存，不能仅按版本号推断其能力。
2. 停止服务，备份完整 workspace、配置、unit 和私有凭据，核验备份可读。备份记录注明当时的应用环境和恢复路径。私有凭据保持原有受限权限，单独保存。
3. 保留旧环境原目录。在 `/opt/framenote` 下准备临时链接，并以重命名原子切换 `current`。下面两条仅用于已经采用版本目录布局的 Linux 服务，目标版本须已验收；`.current-next` 须不存在：

   ```bash
   sudo ln -s releases/v0.2.1 /opt/framenote/.current-next
   sudo mv -Tf /opt/framenote/.current-next /opt/framenote/current
   ```

4. 配置及模型指向与本版兼容的文件，启动服务。核对健康接口的版本、提交、`dirty=false` 和就绪状态，再回读旧任务并提交新任务。
5. 验证失败立即停服，按下一节恢复。切换后继续保留旧版本、发布包、备份和验收记录。

`current` 是应用选择指针，不会切换已经运行的进程，所以必须停服后切换并重新启动。第一次从 `/opt/framenote/.venv` 迁入版本目录布局时，保留该旧环境及旧 unit，按[部署指南](DEPLOYMENT.md)安装新模板；失败时可直接恢复旧 unit 和配置，不要移动旧虚拟环境。

## 回滚与数据兼容

回滚前停止当前服务，并保留当前 workspace、配置和失败日志的快照。随后先检查目标版本能否读取拟交给它的数据，再切换环境和启动。

| 数据情况 | 恢复方式 |
| --- | --- |
| 当前数据经目标版本验证可读，配置与模型也兼容 | 复用 workspace，切回目标版本原目录，并使用匹配配置 / 模型 |
| 包含目标版本不支持的材料或任务状态 | 用升级前备份创建新的恢复 workspace，让旧服务指向该副本；升级后 workspace 完整保留 |

例如 `87e341f` 的早期构建不能读取 v3 字幕材料；`v0.2.0` 和 `v0.2.1` 可以读取 v1 / v2 / v3。软件版本号与材料格式版本是两个不同的标识。版本清单列出已知格式范围；运行队列、操作状态及配置也应在副本上用目标环境检查，不能只比较一个格式数字。

具体恢复顺序：

1. 停止服务并快照当前数据；明确目标发布目录、清单、模型和升级前备份。
2. 核验目标发布包和环境，执行 `pip check`，核对模型锁与模型哈希。
3. 在独立目录检查目标版本读取材料的结果；如果不兼容，复制升级前备份为恢复 workspace，保留升级后全部数据，不覆盖原目录。
4. 切回旧版本目录（使用上述临时链接 + 重命名方式），恢复匹配配置和 unit；配置中的 workspace、模型、ASR Python 必须一致。
5. unit 改动时执行 `systemctl daemon-reload`，启动后检查旧版本身份 / 就绪状态、已有任务和客户端读取结果。

使用备份副本恢复时，旧服务只能看到备份时点的数据。升级后新建的任务仍在保留的 workspace 中，待兼容版本恢复后再处理；这一方式不承诺把新格式任务自动转换成旧格式。

## 隔离演练

每次改变发布流程时，使用实际安装的两个版本执行：旧版启动并读取旧任务 → 新版启动并读取旧 / 新材料 → 检查旧版对新材料的兼容性 → 切回旧版并检查旧任务。对保留的新材料做字节 / 哈希校验，并验证失败构建不能覆盖发布包。

演练使用 loopback、独立端口和 workspace；结果记录在[发布与回滚验收](VALIDATION-RELEASE.md)。演练不等于 Homelab 实际部署验收。

版本格式参考 [Python 包版本说明](https://packaging.python.org/en/latest/discussions/versioning/)。构建身份通过标准 `build_py` / `sdist` 扩展写入，机制参考 [setuptools 构建扩展](https://setuptools.pypa.io/en/latest/userguide/extension.html)。
