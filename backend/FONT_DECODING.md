# 章节与作业字体解码

部分答题页以 `font-cxsecret` 内嵌 TrueType 字体替换少量汉字。HTML 原始字符不是页面显示的正文，配置 SK 无法解决原始文字乱码；先还原文字，再查题库或请求 DeepSeek。

后端只读取当前答题页 `<style>` 中对应字体的 base64 数据，在本地计算字形轮廓指纹，与随包分发的映射表匹配。仅转换标记字体范围内的文本节点；不更改提交控件、选项标签、隐藏字段、Cookie 或签名，不请求远程字体或解码服务。相同字体缓存最多 16 份。数据大小、字体数量、字形数量与点数设限；缺失字体、未知使用字形或矛盾映射明确报错，不把乱码发送给模型，也不改为随机答案。

本地资源 `resources/cxsecret-glyphs.json` 来源于 MIT 项目 [SocialSisterYi/xuexiaoyi-to-xuexitong-tampermonkey-proxy](https://github.com/SocialSisterYi/xuexiaoyi-to-xuexitong-tampermonkey-proxy)，固定提交 `3598d5595d6c4256f3da905dd6244af0699c4dfa` 的 `font_map.json`。原始资源 SHA-256：`792786beba0acc8bdfe4ff7e6f8624bf69e8d4d7d9a72cb6a9453c4ff6cd87e7`。保留 Unicode 字形名，使用 NFKC 统一兼容部首，排除 8 组仍有歧义的映射后，得到 29,048 个唯一字形。派生文件 SHA-256：`307a19af6b5ed128db0a3cbfcacb54264f3a428fdcf644ada638cd0f895d3374`，运行时校验；许可原文随包附于 `resources/FONT_MAP_LICENSE.md`。

运行依赖 `fonttools==4.67.0`，Windows、Ubuntu 和 Docker 从同一 requirements 安装。不需要浏览器、OCR、远程字体服务或用户提供额外凭证。映射范围不覆盖所有可能的字体；无法确认时保持明确失败。
