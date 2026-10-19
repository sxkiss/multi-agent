继续完成当前任务并收尾：
1. 删除旧版重复定义的 gwQrBind/gwQrPollOnce/gwQrClose，统一实例级扫码绑定
2. 补充测试覆盖 instance_id 绑定链路
3. 跑 pytest tests/test_gateway_fix.py tests/test_qr_login.py tests/test_connector_hub.py
4. 隔离测试服务器 + 真实 Chrome 逐页实测
5. 检查工作区残留文件，全部纳入
6. commit 到 dev 分支并 push

完成后用中文写最终总结。
