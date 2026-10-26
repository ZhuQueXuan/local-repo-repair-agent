# Requests JSON exception serialization

公开开发题，来源：[Requests #6628](https://github.com/psf/requests/issues/6628)。固定在修复前的v2.31.0，commit和全部文件指纹见manifest.json。问题描述重新表述，不向Agent提供原帖中的修复建议或参考补丁。

本题用于第17轮检索开发与运行验证。数据公开且可能已被模型训练见过，检查也对Agent可见；不能称为私有留出评测。调用涉及models、exceptions与compat，但不要求补丁修改多个文件，也不把导入邻接当作完整调用图。

6项外部行为检查覆盖直接异常、Response.json异常、不同pickle协议、错误位置、有效JSON及其他异常。仅验证序列化边界，不使用真实HTTP服务或启动进程池。环境使用标准库json，尚不验证可选simplejson后端。

上游针对性与扩展检查仅选本地可运行、不依赖HTTP服务的测试文件，具体目标见manifest.json；不将这些测试称为Requests全量测试。评估报告沿用历史full_upstream_suite字段，当前含义以scope和full_target为准。初次预检有8项因缺少mocker fixture报错，已安装固定pytest-mock==3.14.0并只为本题显式加载；没有安装pytest-httpbin或启动HTTP服务。

repo是冻结原件，运行只在temp新建副本。参考修复不进入索引或工作副本。
