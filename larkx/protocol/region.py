"""飞书(国内版)与 Lark(国际版)的站点差异。

两版共用同一套 web 客户端代码与协议: 接口路径、proto、access_key 算法完全一致,
区别只在域名、web 端 appId(网关请求头 x-appid)和默认语言。登录时选定区域并写入凭证,
之后所有请求都按凭证里的区域取地址(cookie 只对所属域名有效,不能混用)。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    name: str
    label: str
    domain: str
    # 页面 __feishuMeta.teaAppId 的兜底值;登录后会从 messenger 页面抓取实际值存进凭证
    web_app_id: str
    locale: str
    # 头像等静态图片的 CDN, 不需要登录态。国内版实测可用; 国际版的域名未实测
    image_cdn: str = ''

    @property
    def ticket_url(self) -> str:
        return f'https://login.{self.domain}/suite/passport/frontier_ticket/'

    @property
    def api_host(self) -> str:
        return f'https://internal-api-lark-api.{self.domain}'

    @property
    def user_info_url(self) -> str:
        return f'{self.api_host}/accounts/web/user'

    @property
    def csrf_url(self) -> str:
        return f'{self.api_host}/accounts/csrf'

    @property
    def gateway_url(self) -> str:
        return f'{self.api_host}/im/gateway/'

    @property
    def web_origin(self) -> str:
        return f'https://open-dev.{self.domain}'

    @property
    def messenger_page_url(self) -> str:
        return f'{self.web_origin}/messenger/'

    @property
    def qr_redirect_uri(self) -> str:
        return f'{self.web_origin}/next/messenger'

    @property
    def accounts_origin(self) -> str:
        return f'https://accounts.{self.domain}'

    @property
    def ws_url(self) -> str:
        return f'wss://msg-frontier.{self.domain}/ws/v2'

    @property
    def file_host(self) -> str:
        return f'https://internal-api-lark-file.{self.domain}'

    def avatar_url(self, key: str, size: str = '72x72') -> str:
        if not key or not self.image_cdn:
            return ''
        return f'{self.image_cdn}/static-resource/v1/{key}~?image_size={size}&cut_type=&quality=&format=png&sticker_format=.webp'


REGIONS = {
    'feishu': Region(name='feishu', label='飞书', domain='feishu.cn', web_app_id='161471', locale='zh_CN',
                     image_cdn='https://s1-imfile.feishucdn.com'),
    'lark': Region(name='lark', label='Lark', domain='larksuite.com', web_app_id='161471', locale='en_US',
                   image_cdn='https://sf16-sg.larksuitecdn.com'),
}


def get_region(name: str) -> Region:
    region = REGIONS.get((name or '').strip().lower())
    if region is None:
        raise ValueError(f"未知区域: {name!r},可选: {', '.join(REGIONS)}")
    return region
