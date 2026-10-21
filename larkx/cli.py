import argparse
import json
import sys
from datetime import datetime, timedelta
from loguru import logger
from . import state
from .config import is_agent_actor, load_config, with_signature
from .protocol import REGIONS, AuthExpired, LarkAuth, LarkClient, get_region
from .storage import Storage

# 数字人(LARKX_ACTOR=agent)不能用的子命令: 原始网关接口、登录凭证、再起一个监听进程、管理自己的服务、
# 以本人身份给自己下指令(say)、改本机 agent 的 skill
AGENT_FORBIDDEN = {'api', 'auth', 'listen', 'daemon', 'service', 'doctor', 'say', 'skill', 'web'}


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


def _audit(st, action, args_chat, content, msg_id='', ok=True, error=''):
    turn = state.read_turn() if is_agent_actor() else None
    st.log_action(actor='agent' if is_agent_actor() else 'human', action=action, chat_id=str(args_chat or ''),
                  msg_id=str(msg_id or ''), content=content, ok=1 if ok else 0, error=error,
                  turn_kind=(turn or {}).get('kind', ''), turn_ref=(turn or {}).get('ref', ''))


def cmd_send(args):
    text = _read_send_text(args)
    if not text.strip():
        return _fail(args, '消息内容为空')
    cfg = load_config()
    prefix = (cfg.get('triggers') or {}).get('prefix') or ''
    st = Storage()
    agent = is_agent_actor()
    turn = state.read_turn() if agent else None
    client = LarkClient(LarkAuth())
    if agent:
        from .daemon.triggers import strip_mentions
        from .notify import Notifier
        # 允许自己触发时, 以前缀开头的回复会再次触发数字人, 形成自回环
        denied = f'数字人的回复不能以触发前缀 {prefix} 开头' if prefix and strip_mentions(text).startswith(prefix) else ''
        denied = denied or state.send_denied(args.chat_id, turn)
        if not denied:
            n = st.count_actions('agent', 'send', datetime.now() - timedelta(minutes=10))
            if n >= cfg['send_rate_limit']:
                denied = f'数字人 10 分钟内已经发了 {n} 条消息, 达到上限 {cfg["send_rate_limit"]}(LARKX_SEND_RATE_LIMIT)'
        if denied:
            _audit(st, 'send', args.chat_id, text, ok=False, error=denied)
            name = st.get_chat_name(args.chat_id) or args.chat_id
            Notifier(cfg, client).alert(f'拦下了数字人发往「{name}」的消息: {denied}\n内容: {text[:200]}',
                                        key=f'deny:{args.chat_id}:{denied[:20]}', cooldown=600)
            return _fail(args, denied)
        text = with_signature(text, cfg)
    msg_id = client.send_msg(text, args.chat_id, root_id=args.root)
    _audit(st, 'send', args.chat_id, text, msg_id=msg_id, ok=bool(msg_id), error='' if msg_id else '发送失败')
    if not msg_id:
        return _fail(args, '发送失败')
    if agent and cfg.get('mirror'):
        from .notify import Notifier, should_mirror
        notifier = Notifier(cfg, client)
        if should_mirror(args.chat_id, turn, notifier.owner_chat()):
            name = st.get_chat_name(args.chat_id) or args.chat_id
            notifier.send(f'📤 数字人在「{name}」发了:\n{text}', fallback=False)
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
    from .daemon import run_daemon, run_service
    if not sys.stdin.isatty():
        # 无人值守: 登录失效时告警并等待重新登录, 不让 systemd 反复重启
        return run_service()
    return _run_logged_in(run_daemon, need_user_id=True)


def _task_row(t) -> dict:
    return {'id': t.id, 'kind': t.kind, 'status': t.status, 'due_at': datetime.fromtimestamp(t.due_at).strftime('%Y-%m-%d %H:%M:%S') if t.due_at else '',
            'every_sec': t.every_sec, 'chat_id': t.chat_id, 'anchor': t.anchor, 'by_owner': bool(t.by_owner),
            'creator': t.creator, 'run_count': t.run_count, 'prompt': t.prompt, 'result': t.result}


def cmd_recall(args):
    st = Storage()
    sent = st.find_action('agent', 'send', args.msg_id) or st.find_action('human', 'send', args.msg_id)
    if is_agent_actor():
        turn = state.read_turn()
        mine = st.find_action('agent', 'send', args.msg_id)
        if not mine:
            return _fail(args, '数字人只能撤回自己发出的消息')
        if not turn or turn['kind'] == 'system' or (not turn['is_owner'] and mine.chat_id != turn['chat_id']):
            return _fail(args, '只能撤回当前会话里自己发的消息; 其他会话的需要本人发起')
    client = LarkClient(LarkAuth())
    chat_id = sent.chat_id if sent else ''
    try:
        client.api('messages.DeleteMessageRequest', {'id': str(args.msg_id)})
    except Exception as e:
        err = f'撤回失败: {e}'[:300]
        _audit(st, 'recall', chat_id, f'撤回 {args.msg_id}', msg_id=args.msg_id, ok=False, error=err)
        return _fail(args, err + '(只能撤回自己发的消息, 且要在飞书允许的撤回时限内)')
    _audit(st, 'recall', chat_id, f'撤回 {args.msg_id}', msg_id=args.msg_id)
    if args.json:
        _print_json({'ok': True, 'msg_id': args.msg_id})
    else:
        print(f'已撤回 {args.msg_id}')
    return 0


def cmd_takeover(args):
    import time
    st = Storage()
    agent = is_agent_actor()
    turn = state.read_turn() if agent else None
    if args.action == 'list':
        rows = [{'chat_id': t.chat_id, 'chat_name': st.get_chat_name(t.chat_id),
                 'until': datetime.fromtimestamp(t.until).strftime('%Y-%m-%d %H:%M:%S'), 'goal': t.goal, 'creator': t.creator}
                for t in st.takeover_list() if t.until > time.time()]
        if args.json:
            _print_json(rows)
        else:
            for r in rows:
                print(f"{r['chat_name'] or r['chat_id']}  到 {r['until']}  {r['goal']}")
            if not rows:
                print('当前没有接管中的会话')
        return 0
    if not args.chat_id:
        return _fail(args, f'用法: larkx takeover {args.action} <chat_id>')
    if agent and not (turn and turn['is_owner']) and not (args.action == 'stop' and turn and turn['chat_id'] == args.chat_id):
        return _fail(args, '接管会话需要本人发起')
    if args.action == 'stop':
        ok = st.takeover_expire(args.chat_id, time.time())
        msg = '已结束接管, daemon 会在本人私聊发一份总结' if ok else '这个会话当前没有在接管'
    else:
        try:
            seconds = state.parse_duration(args.for_) if args.for_ else load_config()['takeover_default']
        except ValueError as e:
            return _fail(args, str(e))
        until = time.time() + seconds
        st.takeover_start(args.chat_id, until, args.goal or '', 'agent' if agent else 'human')
        ok, msg = True, f"已接管 {st.get_chat_name(args.chat_id) or args.chat_id} 到 {datetime.fromtimestamp(until):%H:%M}"
    if args.json:
        _print_json({'ok': ok, 'message': msg})
    else:
        print(msg)
    return 0 if ok else 1


def cmd_mentions(args):
    import time
    try:
        since = time.time() - state.parse_duration(args.since)
    except ValueError as e:
        return _fail(args, str(e))
    auth = LarkAuth()
    st = Storage()
    rows = st.mentions(auth.user_id, st.owner_name(auth.user_id), int(since))
    if args.json:
        _print_json(rows)
        return 0
    for r in rows:
        when = datetime.fromtimestamp(r['create_time']).strftime('%m-%d %H:%M') if r['create_time'] else ''
        where = '私聊' if r['kind'] == 'p2p' else f"「{r['chat_name'] or r['chat_id']}」@你"
        print(f"{when} {'  ' if r['replied'] else '● '}{where} {r['sender_name']}: {(r['content'] or '')[:80]}")
    if not rows:
        print('这段时间没有人私聊或 @ 你')
    return 0


def cmd_task(args):
    st = Storage()
    agent = is_agent_actor()
    turn = state.read_turn() if agent else None
    if args.action == 'list':
        rows = [_task_row(t) for t in st.list_tasks(include_finished=args.all)]
        if args.json:
            _print_json(rows)
            return 0
        for r in rows:
            every = f' 每{r["every_sec"]}s' if r['every_sec'] else ''
            print(f"#{r['id']} [{r['status']}] {r['due_at']}{every} chat={r['chat_id'] or '-'}: {r['prompt'][:80]}")
        return 0
    if args.action == 'cancel':
        if not args.text or not args.text[0].isdigit():
            return _fail(args, '用法: larkx task cancel <任务 id>')
        t = st.get_task(int(args.text[0]))
        if not t:
            return _fail(args, f'没有任务 #{args.text[0]}')
        if agent and not (turn and turn['is_owner']) and t.chat_id != (turn or {}).get('chat_id'):
            return _fail(args, '只能取消当前会话的任务; 其他任务需要本人发起')
        st.update_task(t.id, status='cancelled')
        if args.json:
            _print_json({'ok': True, 'id': t.id})
        else:
            print(f'已取消任务 #{t.id}')
        return 0
    # add
    prompt = _read_send_text(args)
    if not prompt.strip():
        return _fail(args, '任务内容为空')
    try:
        if args.at:
            due = state.parse_at(args.at)
        elif getattr(args, 'in_'):
            due = datetime.now().timestamp() + state.parse_duration(args.in_)
        else:
            return _fail(args, '需要 --at <时间> 或 --in <时长>')
        every = state.parse_duration(args.every) if args.every else 0
    except ValueError as e:
        return _fail(args, str(e))
    chat_id, anchor, by_owner = args.chat or '', args.root or '', True
    if agent:
        if not turn or turn['kind'] == 'system':
            return _fail(args, '当前没有正在处理的消息, 数字人不能新建任务')
        by_owner = turn['is_owner']
        chat_id = chat_id or turn['chat_id']
        anchor = anchor or (turn['anchor'] if chat_id == turn['chat_id'] else '')
        if chat_id != turn['chat_id'] and not by_owner:
            return _fail(args, '只能给当前会话安排任务; 其他会话需要本人发起')
    task_id = st.add_task(kind='task', due_at=int(due), every_sec=every, prompt=prompt, chat_id=chat_id, anchor=anchor,
                          by_owner=1 if by_owner else 0, creator='agent' if agent else 'human')
    when = datetime.fromtimestamp(due).strftime('%Y-%m-%d %H:%M:%S')
    if args.json:
        _print_json({'ok': True, 'id': task_id, 'due_at': when, 'every_sec': every, 'chat_id': chat_id})
    else:
        print(f'已安排任务 #{task_id}: {when}' + (f', 之后每 {args.every} 一次' if every else ''))
    return 0


def cmd_say(args):
    import time
    text = _read_send_text(args)
    if not text.strip():
        return _fail(args, '内容为空')
    st = Storage()
    task_id = st.add_task(kind='say', due_at=0, prompt=text, by_owner=1, creator='human')
    if not args.wait:
        print(f'已交给数字人(#{task_id}), 结果: larkx task list --all')
        return 0
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        t = st.get_task(task_id)
        if t.status in ('done', 'failed'):
            if args.json:
                _print_json({'ok': t.status == 'done', 'id': t.id, 'result': t.result})
            else:
                print(t.result or '(数字人没有文字回答)')
            return 0 if t.status == 'done' else 1
        time.sleep(1)
    return _fail(args, f'等待超时, 任务 #{task_id} 仍在排队或处理中; daemon 是否在运行? larkx service status')


def cmd_audit(args):
    rows = Storage().list_actions(limit=args.limit)
    if args.json:
        _print_json([{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in rows])
        return 0
    for r in rows:
        mark = '✓' if r.ok else '✗'
        turn = f' ({r.turn_kind} {r.turn_ref})' if r.turn_kind else ''
        detail = (r.content or '').replace('\n', ' ')[:60] + (f' | {r.error}' if r.error else '')
        print(f'{r.created_at:%m-%d %H:%M:%S} {mark} {r.actor}{turn} {r.action} chat={r.chat_id}: {detail}')
    return 0


def cmd_skill(args):
    from .skills import install_user_skill
    targets = ['claude', 'codex'] if args.target == 'all' else [args.target]
    for t in targets:
        path = install_user_skill(t)
        print(f'已安装 larkx skill 到 {t}: {path}')
    return 0


def cmd_service(args):
    import subprocess
    from . import service
    if not service.available():
        return _fail(args, '本机没有 systemd, 无法托管为服务; 可以直接运行 larkx daemon')
    kinds = service.kinds(args.unit)
    if args.action == 'install':
        for path in service.install(args.unit):
            print(f'已安装并设为开机自启: {path}')
        print('启动: larkx service start')
        return 0
    if args.action == 'uninstall':
        service.uninstall(args.unit)
        print('已停止并卸载服务')
        return 0
    if args.action in ('start', 'stop', 'restart'):
        for kind in kinds:
            if not service.unit_path(kind).exists():
                return _fail(args, f'{service.UNITS[kind]} 未安装, 先运行 larkx service install')
            r = service.systemctl(args.action, service.UNITS[kind])
            if r.returncode != 0:
                return _fail(args, f'systemctl {args.action} {service.UNITS[kind]} 失败: {r.stderr.strip()}')
    if args.action == 'logs':
        from .daemon import LOG_PATH
        if not LOG_PATH.exists():
            return _fail(args, f'还没有日志文件 {LOG_PATH}, 服务可能从未成功启动; 可以试试 journalctl --user -u {service.UNIT}')
        cmd = ['tail', '-n', str(args.lines)] + (['-F'] if args.follow else []) + [str(LOG_PATH)]
        return subprocess.run(cmd).returncode
    info = {kind: service.status(kind) for kind in kinds}
    stray = service.stray_daemons()
    if args.json:
        _print_json({'units': info, 'stray_daemons': stray})
        return 0
    for kind, st in info.items():
        label = '运行中' if st['active'] else ('已停止' if st['installed'] else '未安装')
        print(f"{st['unit']}: {label}  pid={st['main_pid'] or '-'}  开机自启={st['enabled']}")
    if stray:
        print(f"注意: 还有不归 systemd 管的 larkx daemon 进程 {stray}, 同一账号会出现两个数字人")
    return 0


def cmd_web(args):
    from .web.app import get_token, login_url, run_web
    cfg = load_config()
    if args.action == 'url':
        print(login_url(cfg))
        return 0
    if args.action == 'reset-token':
        get_token(reset=True)
        print('令牌已重置, 之前登录的浏览器都需要重新打开登录链接:\n' + login_url(cfg))
        return 0
    run_web()
    return 0


def cmd_doctor(args):
    from . import doctor
    checks = doctor.run(skip_model=args.skip_model)
    failed = any(c['level'] == doctor.FAIL for c in checks)
    if args.json:
        _print_json({'ok': not failed, 'checks': checks})
    else:
        mark = {doctor.OK: '✅', doctor.WARN: '⚠️ ', doctor.FAIL: '❌'}
        for c in checks:
            print(f"{mark[c['level']]} {c['name']}: {c['detail']}")
            if c['fix'] and c['level'] != doctor.OK:
                print(f"    → {c['fix']}")
    return 1 if failed else 0


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
    p = sub.add_parser('service', help='用 systemd 用户服务托管 daemon')
    p.add_argument('action', choices=['install', 'uninstall', 'start', 'stop', 'restart', 'status', 'logs'])
    p.add_argument('--unit', choices=['all', 'daemon', 'web'], default='all', help='操作哪个服务, 默认两个都操作')
    p.add_argument('-n', '--lines', type=int, default=100, help='logs 显示的行数')
    p.add_argument('-f', '--follow', action='store_true', help='logs 持续输出')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_service)
    p = sub.add_parser('recall', help='撤回一条自己发出的消息')
    p.add_argument('msg_id')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_recall)
    p = sub.add_parser('takeover', help='接管会话: 一段时间内对方的消息都交给数字人回复')
    p.add_argument('action', choices=['start', 'stop', 'list'])
    p.add_argument('chat_id', nargs='?')
    p.add_argument('--for', dest='for_', help='接管多久, 如 30m、2h; 默认 LARKX_TAKEOVER_DEFAULT')
    p.add_argument('--goal', help='交代给数字人的目标, 如"帮我和他约下周二开会"')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_takeover)
    p = sub.add_parser('mentions', help='最近谁找过你: 别人的私聊、@你的群消息, 以及你之后回没回')
    p.add_argument('--since', default='24h', help='多久以内, 如 12h、1d')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_mentions)
    p = sub.add_parser('task', help='定时任务: 到点后交给数字人处理')
    p.add_argument('action', choices=['add', 'list', 'cancel'])
    p.add_argument('text', nargs='*', help='add: 任务内容(省略或写 - 时从 stdin 读); cancel: 任务 id')
    p.add_argument('--at', help='时间, 如 "2026-10-22 09:00"、"10-22 09:00"、"09:00"')
    p.add_argument('--in', dest='in_', help='多久之后, 如 30m、2h、1d')
    p.add_argument('--every', help='周期, 如 1d、1w; 不填则只执行一次')
    p.add_argument('--chat', help='关联的会话 id(到点时在这里通知)')
    p.add_argument('--root', help='关联的话题 id')
    p.add_argument('--all', action='store_true', help='list: 包括已完成和已取消的')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_task)
    p = sub.add_parser('say', help='以本人身份从终端给数字人下指令')
    p.add_argument('text', nargs='*', help='指令内容; 省略或写 - 时从 stdin 读')
    p.add_argument('--wait', action='store_true', help='等数字人处理完并打印它的回答')
    p.add_argument('--timeout', type=int, default=600)
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_say)
    p = sub.add_parser('audit', help='查看通过 larkx 对外做过的动作(发消息等)')
    p.add_argument('--limit', type=int, default=50)
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_audit)
    p = sub.add_parser('skill', help='把 larkx skill 装进本机的 Claude Code / Codex, 让它们能操作你的飞书')
    p.add_argument('action', choices=['install'])
    p.add_argument('--target', choices=['claude', 'codex', 'all'], default='all')
    p.set_defaults(fn=cmd_skill)
    p = sub.add_parser('web', help='网页控制台: 默认启动服务; url 打印登录链接; reset-token 重置令牌')
    p.add_argument('action', nargs='?', choices=['serve', 'url', 'reset-token'], default='serve')
    p.set_defaults(fn=cmd_web)
    p = sub.add_parser('doctor', help='检查登录、配置、模型调用和服务状态')
    p.add_argument('--skip-model', action='store_true', help='跳过模型调用检查')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_doctor)
    # 在解析参数之前拦截, 参数写错时也不会绕过
    sub_cmd = next((a for a in sys.argv[1:] if not a.startswith('-')), None)
    if is_agent_actor() and sub_cmd in AGENT_FORBIDDEN:
        sys.exit(_fail(argparse.Namespace(json='--json' in sys.argv), f'数字人不能使用 larkx {sub_cmd}', code=3))
    args, extra = parser.parse_known_args()
    if extra and args.cmd in ('send', 'task', 'say'):
        # send 的内容可以写在选项后面, 如 send <chat> --json -
        args.text += extra
    elif extra:
        parser.error(f'无法识别的参数: {" ".join(extra)}')
    if is_agent_actor():
        denied = _guest_denied(args)
        if denied:
            sys.exit(_fail(args, denied, code=3))
    sys.exit(args.fn(args))


def _guest_denied(args) -> str:
    """访客发起的这一轮(不是本人发起): 只能用少数子命令, 只能看当前会话; 被拦下时通知本人。"""
    from . import guest
    turn = state.read_turn()
    if not guest.is_guest(turn):
        return ''
    st = Storage()
    denied = guest.cli_denied(args.cmd, args, turn, st)
    if denied:
        from .notify import Notifier
        st.log_action(actor='agent', action=args.cmd, chat_id=turn.get('chat_id', ''), content=' '.join(sys.argv[1:])[:300],
                      ok=0, error=denied, turn_kind=turn.get('kind', ''), turn_ref=turn.get('ref', ''))
        Notifier(load_config(), LarkClient(LarkAuth())).alert(
            f'访客发起的请求({turn.get("ref")})想让数字人执行 larkx {args.cmd}, 已拦截: {denied}',
            key=f'guest:{args.cmd}:{turn.get("ref")}', cooldown=600)
    return denied


if __name__ == '__main__':
    main()
