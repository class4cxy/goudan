# Roborock 认证说明与安全建议

## 认证用的是谁家的账号？

**是的，用的是小米/石头云账号**，和你在「米家 App」或「Roborock App」里登录的是同一套账号体系。

- 官方文档：[Python Roborock - Usage](https://python-roborock.readthedocs.io/en/latest/usage.html)
- 仓库（新版本）：[humbertogontijo/python-roborock](https://github.com/humbertogontijo/python-roborock)（PyPI 上 4.x 版本）

SDK 通过 **Roborock/小米云 API** 做两件事：
1. 用账号登录云端，拿到会话（token）
2. 用该会话拉取设备列表、下发指令（或经 MQTT/本地协议与机器人通信）

所以 `ROBOROCK_USERNAME` 填的是**小米账号邮箱**，`ROBOROCK_PASSWORD` 是**该账号的密码**。

---

## 明文密码放在 .env 里安全吗？

**有风险，不建议长期把明文密码放在仓库或共享环境里。** 建议至少做到：

1. **不要把 `.env` 提交到 Git**  
   项目已包含 `.env` 在 `.gitignore` 中（若没有请自行添加）。

2. **限制 .env 权限**  
   ```bash
   chmod 600 .env
   ```

3. **生产/共享环境**  
   用系统环境变量、密钥管理（如 macOS Keychain、HashiCorp Vault）或 CI Secrets 注入，而不是把 `.env` 文件拷来拷去。

4. **优先使用「验证码登录 + Token」**（见下），减少明文密码的使用场景。

---

## 更安全的方式：验证码登录 + Token 持久化

python-roborock 支持两种登录方式：

| 方式 | 说明 | 安全程度 |
|------|------|----------|
| **pass_login(password)** | 邮箱 + 密码，当前 Bridge 默认使用 | 需在环境里存明文密码 |
| **request_code() + code_login(code)** | 向邮箱发验证码，用码换 Token | 不在环境里存密码，仅一次性输入验证码 |

**推荐做法**（适合本机或自建服务器）：

1. **一次性**在本地运行「验证码登录」流程，把返回的 **user_data（即登录态/Token）** 保存到本地文件。
2. Bridge 启动时**优先从该文件读取 Token**，只有文件不存在或失效时才回退到「密码登录」。

这样日常运行不再需要 `ROBOROCK_PASSWORD`，只需保留 `ROBOROCK_USERNAME` 和 Token 文件；Token 泄露的影响也小于密码泄露。

### 验证码登录流程（官方示例）

```python
from roborock.web_api import RoborockApiClient

email = "your@email.com"
api = RoborockApiClient(username=email)

# 1. 请求验证码（会发到你的邮箱）
await api.request_code()

# 2. 在邮箱里拿到验证码，输入
code = input("请输入邮箱收到的验证码: ")
user_data = await api.code_login(code)

# 3. user_data 即为登录态，可序列化保存后供 Bridge 使用
#    具体序列化方式取决于库版本（如 .model_dump()、.dict() 等）
```

当前 Bridge 实现的是 **pass_login**。若你希望改为「仅用 Token 文件、不落盘密码」，可以：

- 在 Bridge 里增加：**若存在 `ROBOROCK_CACHE_PATH` 且文件有效，则从文件加载 login_data，否则再走 pass_login**；  
- 或单独写一个 `platform/login_once.py`，跑通 `request_code` → `code_login`，把 `user_data` 按库支持的格式写入 `ROBOROCK_CACHE_PATH`，再由 Platform 读取。

具体字段名和序列化方式需对照你安装的 python-roborock 版本（4.x 的 `UserData` / 返回值结构）做适配。

---

## 小结

| 问题 | 答案 |
|------|------|
| 认证是否用小米账号？ | 是，和米家/Roborock App 同款云账号。 |
| 有没有官方文档？ | 有，[Python Roborock 官方文档](https://python-roborock.readthedocs.io/en/latest/usage.html)。 |
| 传明文密码是否安全？ | 有风险；.env 不要进 Git，限制权限，生产用密钥管理或改用验证码+Token。 |
| 更安全的做法？ | 使用验证码登录一次，把返回的 Token/user_data 持久化，Bridge 优先读 Token 文件。 |
