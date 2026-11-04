"""优-1 路由拆分回归：抽离后路由仍然存在且不重复注册。"""

EXPECTED = {
    ("GET", "/api/files/raw"),
    ("GET", "/api/files/browse"),
    ("POST", "/api/files/upload"),
    ("GET", "/api/backups"),
    ("POST", "/api/backups/create"),
    ("POST", "/api/backups/restore"),
    ("POST", "/api/backups/auto"),
    ("GET", "/api/knowledge/list"),
    ("POST", "/api/knowledge/upload"),
    ("POST", "/api/knowledge/add_text"),
    ("POST", "/api/knowledge/search"),
    ("POST", "/api/knowledge/delete"),
    ("POST", "/api/knowledge/add_mem0"),
    ("GET", "/api/config"),
    ("POST", "/api/config"),
    ("GET", "/api/tools"),
    ("GET", "/api/skills"),
    ("GET", "/api/mcp/servers"),
    ("GET", "/api/crew/agents"),
    ("POST", "/api/hotreload"),
    ("POST", "/api/restart"),
}


def test_extracted_route_modules_registered_once():
    import web_server

    routes = [
        (method, getattr(r, "path", ""))
        for r in web_server.app.routes
        for method in (getattr(r, "methods", None) or {"GET"})
    ]
    for method, path in sorted(EXPECTED):
        assert routes.count((method, path)) == 1, (
            f"{method} {path} 注册次数异常: {routes.count((method, path))}"
        )
