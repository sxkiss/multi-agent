"""优-1 路由拆分回归：抽离后路由仍然存在且不重复注册。"""


def test_extracted_route_modules_registered_once():
    import web_server

    paths = [getattr(r, "path", "") for r in web_server.app.routes]
    expected = {
        "/api/files/raw",
        "/api/files/browse",
        "/api/files/upload",
        "/api/backups",
        "/api/backups/create",
        "/api/backups/restore",
        "/api/backups/auto",
        "/api/knowledge/list",
        "/api/knowledge/upload",
        "/api/knowledge/add_text",
        "/api/knowledge/search",
        "/api/knowledge/delete",
        "/api/knowledge/add_mem0",
    }
    for path in expected:
        assert paths.count(path) == 1, f"{path} 注册次数异常: {paths.count(path)}"
