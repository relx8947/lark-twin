import argparse
import json
import sys
from datetime import datetime
from loguru import logger
from .config import is_agent_actor, load_config
from .protocol import REGIONS, AuthExpired, LarkAuth, LarkClient, get_region
from .storage import Storage

# 数字人(LARKX_ACTOR=agent)不能用的子命令: 原始网关接口、登录凭证、再起一个监听进程
AGENT_FORBIDDEN = {'api', 'auth', 'listen', 'daemon'}


def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _fail(args, msg: str, code: int=1) -> int:
    if getattr(args, 'json', False):
        _print_json({'ok': False, 'error': msg})
    else:
        print(msg, file=sys.stderr)
    return code


def cmd_auth(args):
    auth = LarkAuth()
    # 新登录的区域: --region > 已有凭证的区域 > LARKX_REGION
    region = get_region(args.region) if args.region else auth.region
    if args.action == 'status':
        _print_json(auth.status())
        return 0
    if args.action == 'import':
        if args.cookie:
            cookie_str = args.cookie
        elif args.file:
            cookie_str = open(args.file, encoding='utf-8').read()
        else:
            logger.info('请粘贴 cookie 串,回车结束:')
            cookie_str = sys.stdin.readline()
        try:
            msg = auth.import_cookie_string(cookie_str, region)
            print(f'导入成功({region.name}): {msg}')
            return 0
        except (ValueError, AuthExpired) as e:
            print(f'导入失败: {e}', file=sys.stderr)
            return 1
    if args.action == 'check':
        ok, msg = auth.validate(fetch_profile=True)
        if ok:
            auth.save()
        print(('OK ' if ok else 'EXPIRED ') + msg)
        return 0 if ok else 1
    if args.action == 'qr':
        try:
            import qrcode as _qrcode
        except ImportError:
            print('需要 qrcode 包: pip install qrcode', file=sys.stderr)
            return 1
        from .protocol.auth import QR_STATUS_NAMES, QrLogin
        from .config import DATA_DIR
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        png_path = DATA_DIR / 'login_qr.png'

        def on_status(status, step):
            print(f'状态: {QR_STATUS_NAMES.get(status, status)} ({step})', flush=True)

        for attempt in range(1, 7):
            try:
                ql = QrLogin(region)
            except AuthExpired as e:
                print(str(e), file=sys.stderr)
                return 1
            qr = _qrcode.QRCode(border=1)
            qr.add_data(ql.qr_content)
            qr.make_image().save(str(png_path))
            print(f'[{attempt}/6] 二维码已刷新(请用 {region.label} App 扫码): {png_path}', flush=True)
            if attempt == 1:
                try:
                    qr.print_ascii(invert=True)
                except Exception:
                    qr.print_tty()
                sys.stdout.flush()
            ok, data = ql.wait(timeout=45, on_status=on_status)
            if ok:
                try:
                    msg = ql.finish(auth)
                    print(f'登录成功,凭证已保存: {msg}')
                    return 0
                except AuthExpired as e:
                    print(str(e), file=sys.stderr)
                    return 1
            print('当前二维码已失效,自动刷新...', flush=True)
        print('多次刷新后仍未完成登录,请重试', file=sys.stderr)
        return 1


def _ask_login_choice():
    try:
        import questionary
        ans = questionary.select(
            '选择登录方式:',
            choices=[
                questionary.Choice('扫码登录(推荐)', value='qr'),
                questionary.Choice('粘贴 cookie 登录', value='cookie'),
                questionary.Choice('退出', value='quit'),
            ],
        ).ask()
        if ans:
            return ans
    except Exception:
        pass
    print('  [1] 扫码登录(推荐)')
    print('  [2] 粘贴 cookie 登录')
    print('  [q] 退出')
    c = input('选择: ').strip().lower()
    return {'1': 'qr', '2': 'cookie'}.get(c, 'quit')


def _ask_cookie():
    try:
        import questionary
        ans = questionary.text('粘贴完整 cookie 后回车:').ask()
        if ans is not None:
            return ans
    except Exception:
        pass
    return input('粘贴完整 cookie 后回车: ').strip()


def _interactive_login():
    auth = LarkAuth()
    ok, msg = auth.validate(fetch_profile=True)
    if ok:
        auth.save()
        return auth
    if not sys.stdin.isatty():
        raise AuthExpired(msg)
    print(f'凭证缺失或已失效(区域: {auth.region.name},切换区域用 larkx auth qr --region <feishu|lark>): {msg}', flush=True)
    while True:
        choice = _ask_login_choice()
        if choice == 'qr':
            rc = cmd_auth(argparse.Namespace(action='qr', cookie=None, file=None, region=None))
            if rc == 0:
                auth2 = LarkAuth()
                ok2, _ = auth2.validate()
                if ok2:
                    return auth2
        elif choice == 'cookie':
            cookie_str = _ask_cookie()
            if cookie_str is None:
                raise SystemExit(1)
            try:
                auth.import_cookie_string(cookie_str)
                return auth
            except Exception as e:
                print(f'导入失败: {e}')
        else:
            raise SystemExit(1)


def _read_send_text(args) -> str:
    # 省略内容或写 - 时从 stdin 读: 多行和含引号、$ 的内容不经过 shell 参数拼接
    if not args.text or args.text == ['-']:
        if sys.stdin.isatty():
            return ''
        return sys.stdin.read().rstrip('\n')
    return ' '.join(args.text)


def cmd_send(args):
    text = _read_send_text(args)
    if not text.strip():
        return _fail(args, '消息内容为空')
    prefix = (load_config().get('triggers') or {}).get('prefix') or ''
    if is_agent_actor() and prefix and text.lstrip().startswith(prefix):
        # 允许自己触发时, 以前缀开头的回复会再次触发数字人, 形成自回环
        return _fail(args, f'数字人的回复不能以触发前缀 {prefix} 开头')
    client = LarkClient(LarkAuth())
    msg_id = client.send_msg(text, args.chat_id, root_id=args.root)
    if not msg_id:
        return _fail(args, '发送失败')
    st = Storage()
    # 用服务端 id 落库,监听收到回显时会补全这条而不是再插一条
    st.save_message({'msg_id': msg_id, 'chat_id': args.chat_id, 'chat_type': 0, 'scope': 'topic' if args.root else 'chat', 'anchor': args.root or '', 'from_id': client.auth.user_id, 'msg_type': 4, 'msg_type_name': 'TEXT', 'content': text, 'create_time': int(datetime.now().timestamp())}, sender_name='我', direction='out')
    if args.json:
        _print_json({'ok': True, 'msg_id': msg_id, 'chat_id': args.chat_id})
    else:
        print(f'已发送 msg_id={msg_id}')
    return 0


def cmd_config(args):
    cfg = load_config()
    _print_json(cfg)
    return 0


def cmd_chats(args):
    st = Storage()
    rows = st.list_chats(limit=args.limit)
    if args.json:
        _print_json([{'chat_id': c.chat_id, 'name': c.name, 'chat_type': c.chat_type, 'updated_at': c.updated_at} for c in rows])
        return 0
    for c in rows:
        type_name = {1: '私聊', 2: '群聊', 3: '话题群'}.get(c.chat_type, '?')
        print(f"{c.chat_id}\t{type_name}\t{c.name or '(未命名)'}\t更新于 {c.updated_at}")
    return 0


def cmd_sessions(args):
    st = Storage()
    rows = st.list_sessions(limit=args.limit)
    for r in rows:
        scope = '话题' if r.anchor else ('全局' if r.session_key == 'global' else '会话')
        started = '已建立' if r.started else '待建立'
        print(f"{r.session_key}\t{scope}\tchat={r.chat_id}\tanchor={r.anchor or '-'}\t{started}\tmsg={r.msg_count}\t活跃于 {r.last_active}")
    return 0


def cmd_messages(args):
    st = Storage()
    anchor = None
    if args.anchor is not None:
        anchor = args.anchor
    rows = st.get_messages(args.chat_id, anchor=anchor, limit=args.limit)
    if args.json:
        _print_json([{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in rows])
        return 0
    for r in rows:
        ts = datetime.fromtimestamp(r.create_time).strftime('%m-%d %H:%M') if r.create_time else ''
        scope = f'[{r.scope}:{r.anchor[:8]}]' if r.anchor else ''
        content = r.content or ''
        shown = content[:120] + ('…' if len(content) > 120 else '')
        print(f'[{ts}]{scope} {r.sender_name or r.sender_id}: {shown}')
    return 0


def cmd_search(args):
    client = LarkClient(LarkAuth())
    results = client.search(' '.join(args.query))
    _print_json(results)
    return 0


def cmd_api(args):
    client = LarkClient(LarkAuth())
    data = None
    if args.json_body:
        try:
            data = json.loads(args.json_body)
        except Exception as e:
            print(f'JSON 解析失败: {e}', file=sys.stderr)
            return 1
    try:
        resp = client.api(args.type, data)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    _print_json(resp)
    return 0


def cmd_history(args):
    client = LarkClient(LarkAuth())
    chat_id = args.chat_id
    if args.start is not None:
        start = args.start
    else:
        info = client.get_chat_info(chat_id)
        if not info or not info.get('lastMessagePosition'):
            print('拿不到会话的最新 position,请用 --start 指定', file=sys.stderr)
            return 1
        start = int(info['lastMessagePosition']) - args.count + 1
        if info.get('name'):
            st = Storage()
            st.update_chat_name(chat_id, info['name'], info.get('type', 0) if isinstance(info.get('type'), int) else 0)
    positions = list(range(max(1, start), max(1, start) + args.count))
    st = Storage() if args.save else None
    msgs = client.pull_history(chat_id, positions, save_storage=st)
    for m in msgs:
        if args.json:
            continue
        ts = datetime.fromtimestamp(m['create_time']).strftime('%m-%d %H:%M') if m.get('create_time') else ''
        scope = f"[{m['scope']}:{m['anchor'][:8]}]" if m['anchor'] else ''
        content = m['content'] or ''
        shown = content[:120] + ('…' if len(content) > 120 else '')
        print(f"{m['position']:>6} [{ts}]{scope} {m['from_id']}: {shown}")
    if args.json:
        _print_json([{k: v for k, v in m.items() if k != 'content_data'} for m in msgs])
    if args.save:
        print(f'已入库 {len(msgs)} 条')
    return 0


def cmd_download(args):
    from .protocol.media import download, extract_resource_key, message_resource_url
    auth = LarkAuth()
    auth.require_valid()
    file_host = auth.region.file_host
    if args.key:
        if not args.chat:
            print('--key 需要配合 --chat <chat_id>', file=sys.stderr)
            return 1
        url = message_resource_url(args.msg_id or '0', args.key, args.chat, host=file_host)
        msg = None
    else:
        st = Storage()
        rows = None
        if args.chat:
            rows = st.get_messages(args.chat, limit=500)
            rows = [r for r in rows if r.msg_id == args.msg_id]
        else:
            from .storage.models import Message
            s = st.Session()
            rows = s.query(Message).filter_by(msg_id=args.msg_id).all()
            s.close()
        if not rows:
            print(f'本地未找到消息 {args.msg_id}(可用 --key/--chat 直接指定)', file=sys.stderr)
            return 1
        r = rows[-1]
        msg = {'msg_id': r.msg_id, 'chat_id': r.chat_id, 'msg_type': r.msg_type, 'content_data': json.loads(r.content_data) if r.content_data else None}
        key = extract_resource_key(r.msg_type, msg['content_data'] or {})
        if not key:
            print(f'消息 {args.msg_id} 类型为 {r.msg_type_name},没有可下载资源', file=sys.stderr)
            return 1
        url = message_resource_url(r.msg_id, key, r.chat_id, host=file_host)
    out = args.out
    if not out:
        print('必须指定 -o/--out 保存路径', file=sys.stderr)
        return 1
    path, content_type, n = download(auth, url, out)
    print(f'已下载: {path} ({content_type}, {n} 字节)')
    return 0


def _run_logged_in(fn, need_user_id=False):
    try:
        while True:
            try:
                auth = _interactive_login()
                if need_user_id and not auth.user_id:
                    auth._fetch_profile()
                    auth.save()
                if need_user_id and not auth.user_id:
                    print('无法确定当前账号 user_id, 数字人存在自回环风险, 已拒绝启动。请先 larkx auth check 确认凭证有效。', file=sys.stderr)
                    return 1
                return fn(auth)
            except AuthExpired as e:
                if not sys.stdin.isatty():
                    print(str(e), file=sys.stderr)
                    return 1
                print(f'运行中凭证失效: {e}', flush=True)
    except KeyboardInterrupt:
        print('\n已停止')
        return 0


def cmd_listen(args):
    from .daemon import run_listen
    return _run_logged_in(run_listen)


def cmd_daemon(args):
    from .daemon import run_daemon
    return _run_logged_in(run_daemon, need_user_id=True)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    logger.remove()
    logger.add(sys.stderr, level='INFO')
    parser = argparse.ArgumentParser(prog='larkx', description='以本人身份操作飞书 / Lark 的 CLI, 也是数字人的工具')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('auth', help='凭证管理')
    p.add_argument('action', choices=['status', 'import', 'check', 'qr'])
    p.add_argument('--cookie', help='直接传 cookie 串')
    p.add_argument('--file', help='从文件读 cookie')
    p.add_argument('--region', choices=list(REGIONS), help='qr/import 登录的区域: feishu=飞书(国内版) lark=Lark(国际版);默认沿用已有凭证,无凭证时取 LARKX_REGION')
    p.set_defaults(fn=cmd_auth)
    p = sub.add_parser('send', help='发送文本消息')
    p.add_argument('chat_id')
    p.add_argument('text', nargs='*', help='消息内容; 省略或写 - 时从 stdin 读取')
    p.add_argument('--root', help='话题/线程根消息 id,回复进话题')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_send)
    p = sub.add_parser('config', help='查看生效中的配置(配置项写在 .env 里)')
    p.set_defaults(fn=cmd_config)
    p = sub.add_parser('chats', help='列出本地会话')
    p.add_argument('--limit', type=int, default=50)
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_chats)
    p = sub.add_parser('sessions', help='列出 agent 会话(session 表)')
    p.add_argument('--limit', type=int, default=50)
    p.set_defaults(fn=cmd_sessions)
    p = sub.add_parser('messages', help='读本地消息记录')
    p.add_argument('chat_id')
    p.add_argument('--anchor', default=None, help='话题 id;空字符串=仅主会话流')
    p.add_argument('--limit', type=int, default=50)
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_messages)
    p = sub.add_parser('search', help='搜索用户/群')
    p.add_argument('query', nargs='+')
    p.set_defaults(fn=cmd_search)
    p = sub.add_parser('api', help='调任意网关接口(类型名见 docs/cmd_table.md)')
    p.add_argument('type', help='proto 请求类型名(如 chats.PullChatsByIdsRequest)或 cmd 数字')
    p.add_argument('json_body', nargs='?', help='请求字段 JSON')
    p.set_defaults(fn=cmd_api)
    p = sub.add_parser('history', help='拉取会话历史消息')
    p.add_argument('chat_id')
    p.add_argument('--start', type=int, help='起始 position(默认从最新往前)')
    p.add_argument('--count', type=int, default=20)
    p.add_argument('--save', action='store_true', help='同时写入本地库')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_history)
    p = sub.add_parser('download', help='下载消息里的图片/文件等资源')
    p.add_argument('msg_id', nargs='?', help='消息 id(从本地库查资源 key)')
    p.add_argument('--key', help='直接指定资源 key(img_v3_*/file_v3_*),需配合 --chat')
    p.add_argument('--chat', help='chat_id(配合 --key 或加速本地查找)')
    p.add_argument('-o', '--out', required=True, help='保存路径')
    p.set_defaults(fn=cmd_download)
    p = sub.add_parser('listen', help='常驻接收消息, 只入库不接数字人')
    p.set_defaults(fn=cmd_listen)
    p = sub.add_parser('daemon', help='常驻运行数字人: 收消息并交给一直开着的 agent 会话')
    p.set_defaults(fn=cmd_daemon)
    # 在解析参数之前拦截, 参数写错时也不会绕过
    sub_cmd = next((a for a in sys.argv[1:] if not a.startswith('-')), None)
    if is_agent_actor() and sub_cmd in AGENT_FORBIDDEN:
        sys.exit(_fail(argparse.Namespace(json='--json' in sys.argv), f'数字人不能使用 larkx {sub_cmd}', code=3))
    args, extra = parser.parse_known_args()
    if extra and args.cmd == 'send':
        # send 的内容可以写在选项后面, 如 send <chat> --json -
        args.text += extra
    elif extra:
        parser.error(f'无法识别的参数: {" ".join(extra)}')
    sys.exit(args.fn(args))


if __name__ == '__main__':
    main()
