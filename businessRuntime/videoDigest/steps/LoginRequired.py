"""视频站点鉴权异常。无注册参数；提示部署者提供服务器自身Cookie，不打开用户本机浏览器。"""


class LoginRequired(RuntimeError):
    """缺少站点登录凭据时使用的明确异常。"""
