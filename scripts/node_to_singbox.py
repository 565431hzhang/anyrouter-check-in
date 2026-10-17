#!/usr/bin/env python3
"""把节点分享链接转换成 sing-box 配置（本地 mixed 入站 + 单个出站）。

用法:  NODE_LINK=<链接> PROXY_PORT=7890 python3 node_to_singbox.py <输出 config.json 路径>

支持协议: hysteria2 / hy2、vless（含 reality）、vmess、trojan、shadowsocks(ss)、tuic
支持传输: tcp、ws、grpc、http(h2)、httpupgrade
成功时向 stdout 打印节点摘要（服务器地址已打码），失败时以非 0 退出并给出原因。
"""

from __future__ import annotations

import base64
import json
import os
import sys
from urllib.parse import parse_qs, unquote, urlsplit

SUPPORTED = 'hysteria2/hy2、vless、vmess、trojan、ss、tuic'


class NodeError(Exception):
	pass


def b64decode(data: str) -> bytes:
	data = unquote(data).strip().replace('-', '+').replace('_', '/')
	data += '=' * (-len(data) % 4)
	return base64.b64decode(data)


def truthy(v: str | None) -> bool:
	return str(v or '').lower() in ('1', 'true', 'yes')


def mask_host(host: str) -> str:
	parts = host.split('.')
	if len(parts) == 4 and all(p.isdigit() for p in parts):
		return f'{parts[0]}.{parts[1]}.*.*'
	if ':' in host:
		return ':'.join(host.split(':')[:2]) + ':*:*'
	return host[:3] + '***' if len(host) > 3 else '***'


def csv(v: str | None) -> list[str] | None:
	items = [x for x in (v or '').split(',') if x]
	return items or None


def build_tls(q: dict, host: str, *, default_enabled: bool = False, security: str | None = None) -> dict | None:
	"""通用 TLS / reality 配置。security: tls | reality | none | None(按 default_enabled)"""
	security = (security or q.get('security') or '').lower()
	if security in ('', 'none') and not default_enabled:
		return None
	if security == 'none':
		return None
	tls: dict = {'enabled': True, 'server_name': q.get('sni') or q.get('peer') or q.get('host') or host}
	if truthy(q.get('allowInsecure')) or truthy(q.get('insecure')) or truthy(q.get('allow_insecure')):
		tls['insecure'] = True
	alpn = csv(q.get('alpn'))
	if alpn:
		tls['alpn'] = alpn
	fp = q.get('fp')
	if fp and fp != 'none':
		tls['utls'] = {'enabled': True, 'fingerprint': fp}
	if security == 'reality':
		if not q.get('pbk'):
			raise NodeError('reality 节点缺少 pbk（public key）')
		tls['reality'] = {'enabled': True, 'public_key': q['pbk'], 'short_id': q.get('sid', '')}
		tls.setdefault('utls', {'enabled': True, 'fingerprint': 'chrome'})
	return tls


def build_transport(kind: str | None, q: dict) -> dict | None:
	kind = (kind or 'tcp').lower()
	host = q.get('host') or ''
	path = unquote(q.get('path') or '') or '/'
	if kind in ('tcp', 'raw', ''):
		if q.get('headerType') == 'http':
			raise NodeError('不支持 tcp + http 伪装头（headerType=http）')
		return None
	if kind == 'ws':
		t: dict = {'type': 'ws', 'path': path}
		if host:
			t['headers'] = {'Host': host}
		return t
	if kind == 'grpc':
		return {'type': 'grpc', 'service_name': unquote(q.get('serviceName') or q.get('path') or '')}
	if kind in ('http', 'h2'):
		t = {'type': 'http', 'path': path}
		if host:
			t['host'] = host.split(',')
		return t
	if kind == 'httpupgrade':
		t = {'type': 'httpupgrade', 'path': path}
		if host:
			t['host'] = host
		return t
	raise NodeError(f'不支持的传输方式：{kind}（支持 tcp / ws / grpc / http / httpupgrade）')


def need(u, what: str = '服务器 / 端口'):
	if not (u.hostname and u.port):
		raise NodeError(f'节点链接不完整：缺少 {what}')


def parse_hysteria2(u, q):
	need(u)
	password = unquote(u.username or '')
	if u.password:
		password += ':' + unquote(u.password)
	if not password:
		raise NodeError('hysteria2 链接缺少密码')
	tls = build_tls(q, u.hostname, default_enabled=True, security='tls')
	out = {'type': 'hysteria2', 'server': u.hostname, 'server_port': u.port, 'password': password, 'tls': tls}
	if truthy(q.get('insecure')):
		tls['insecure'] = True
	if q.get('obfs'):
		out['obfs'] = {'type': q['obfs'], 'password': q.get('obfs-password', '')}
	notes = []
	if q.get('pinSHA256'):
		notes.append('链接带有 pinSHA256：sing-box 不支持证书指纹固定，已忽略')
	return out, notes


def parse_vless(u, q):
	need(u)
	if not u.username:
		raise NodeError('vless 链接缺少 UUID')
	out = {'type': 'vless', 'server': u.hostname, 'server_port': u.port, 'uuid': unquote(u.username)}
	if q.get('flow'):
		out['flow'] = q['flow']
	tls = build_tls(q, u.hostname)
	if tls:
		out['tls'] = tls
	transport = build_transport(q.get('type'), q)
	if transport:
		out['transport'] = transport
	return out, []


def parse_vmess(raw: str):
	try:
		d = json.loads(b64decode(raw[len('vmess://'):].split('#')[0]).decode('utf-8'))
	except Exception as e:
		raise NodeError(f'vmess 链接解析失败：{e}') from e
	host_ = d.get('add')
	try:
		port = int(d.get('port'))
	except (TypeError, ValueError):
		port = None
	if not (host_ and port and d.get('id')):
		raise NodeError('vmess 链接不完整：缺少 add / port / id')
	out: dict = {
		'type': 'vmess',
		'server': host_,
		'server_port': port,
		'uuid': d['id'],
		'security': d.get('scy') or 'auto',
		'alter_id': int(d.get('aid') or 0),
	}
	q = {
		'sni': d.get('sni') or '',
		'host': d.get('host') or '',
		'path': d.get('path') or '',
		'alpn': d.get('alpn') or '',
		'fp': d.get('fp') or '',
		'headerType': d.get('type') if d.get('type') not in (None, 'none', '') else '',
		'serviceName': d.get('path') or '',
	}
	if (d.get('tls') or '').lower() == 'tls':
		tls = build_tls(q, host_, security='tls')
		if truthy(str(d.get('allowInsecure', ''))):
			tls['insecure'] = True
		out['tls'] = tls
	transport = build_transport(d.get('net'), q)
	if transport:
		out['transport'] = transport
	return out, [], d.get('ps') or ''


def parse_trojan(u, q):
	need(u)
	if not u.username:
		raise NodeError('trojan 链接缺少密码')
	out = {'type': 'trojan', 'server': u.hostname, 'server_port': u.port, 'password': unquote(u.username)}
	security = (q.get('security') or 'tls').lower()
	tls = build_tls(q, u.hostname, default_enabled=True, security=security)
	if tls:
		out['tls'] = tls
	transport = build_transport(q.get('type'), q)
	if transport:
		out['transport'] = transport
	return out, []


def parse_ss(raw: str):
	body = raw[len('ss://'):]
	body, _, frag = body.partition('#')
	body, _, query = body.partition('?')
	q = {k: v[0] for k, v in parse_qs(query).items()}
	if q.get('plugin'):
		raise NodeError('不支持带 plugin 的 ss 节点（如 obfs / v2ray-plugin）')
	if '@' in body:
		userinfo, _, hostport = body.rpartition('@')
		if ':' not in unquote(userinfo):
			try:
				userinfo = b64decode(userinfo).decode('utf-8')
			except Exception as e:
				raise NodeError(f'ss 链接解析失败：{e}') from e
		else:
			userinfo = unquote(userinfo)
	else:
		try:
			decoded = b64decode(body).decode('utf-8')
		except Exception as e:
			raise NodeError(f'ss 链接解析失败：{e}') from e
		userinfo, _, hostport = decoded.rpartition('@')
	method, _, password = userinfo.partition(':')
	host_, _, port_s = hostport.rpartition(':')
	host_ = host_.strip('[]')
	if not (method and password and host_ and port_s.isdigit()):
		raise NodeError('ss 链接不完整：缺少 加密方式 / 密码 / 服务器 / 端口')
	out = {'type': 'shadowsocks', 'server': host_, 'server_port': int(port_s), 'method': method, 'password': password}
	return out, [], unquote(frag)


def parse_tuic(u, q):
	need(u)
	if not (u.username and u.password):
		raise NodeError('tuic 链接需要 uuid:password')
	tls = build_tls(q, u.hostname, default_enabled=True, security='tls')
	out = {
		'type': 'tuic',
		'server': u.hostname,
		'server_port': u.port,
		'uuid': unquote(u.username),
		'password': unquote(u.password),
		'congestion_control': q.get('congestion_control') or q.get('congestion-controller') or 'bbr',
		'udp_relay_mode': q.get('udp_relay_mode') or q.get('udp-relay-mode') or 'native',
		'tls': tls,
	}
	if not tls.get('alpn'):
		tls['alpn'] = ['h3']
	return out, []


def convert(link: str):
	"""返回 (outbound, 备注名, 提示列表)"""
	link = link.strip()
	if '://' not in link:
		raise NodeError(f'无法识别的节点链接（应以协议头开头，支持：{SUPPORTED}）')
	scheme = link.split('://', 1)[0].lower()

	if scheme == 'vmess':
		out, notes, name = parse_vmess(link)
		return out, name, notes
	if scheme == 'ss':
		out, notes, name = parse_ss(link)
		return out, name, notes

	u = urlsplit(link)
	q = {k: v[0] for k, v in parse_qs(u.query).items()}
	name = unquote(u.fragment)
	parsers = {
		'hysteria2': parse_hysteria2,
		'hy2': parse_hysteria2,
		'vless': parse_vless,
		'trojan': parse_trojan,
		'tuic': parse_tuic,
	}
	if scheme not in parsers:
		raise NodeError(f'不支持的节点协议：{scheme}（目前支持：{SUPPORTED}）')
	out, notes = parsers[scheme](u, q)
	return out, name, notes


def main() -> int:
	if len(sys.argv) != 2:
		print('用法：node_to_singbox.py <config.json>', file=sys.stderr)
		return 2
	link = os.environ.get('NODE_LINK', '').strip()
	port = int(os.environ.get('PROXY_PORT', '7890'))
	try:
		outbound, name, notes = convert(link)
	except NodeError as e:
		print(f'❌ {e}')
		return 1

	outbound['tag'] = 'proxy'
	config = {
		'log': {'level': 'warn', 'timestamp': True},
		'inbounds': [{'type': 'mixed', 'tag': 'mixed-in', 'listen': '127.0.0.1', 'listen_port': port}],
		'outbounds': [outbound, {'type': 'direct', 'tag': 'direct'}],
		'route': {'final': 'proxy'},
	}
	with open(sys.argv[1], 'w', encoding='utf-8') as f:
		json.dump(config, f, ensure_ascii=False, indent=2)

	# 衍生的敏感片段补充打码（Secret 整串已被 GitHub 自动打码）
	for secret in (outbound.get('password'), outbound.get('uuid'), outbound['server']):
		if secret:
			print(f'::add-mask::{secret}')

	tls = outbound.get('tls') or {}
	transport = (outbound.get('transport') or {}).get('type', 'tcp')
	security = 'reality' if tls.get('reality') else ('tls' if tls else '无')
	print(f'🏷️  节点备注：{name or "（无）"}')
	print(f'🔗 协议：{outbound["type"]}  服务器：{mask_host(outbound["server"])}:{outbound["server_port"]}')
	print(f'🔒 传输：{transport}  TLS：{security}' + (f'  SNI：{tls["server_name"]}' if tls.get('server_name') else ''))
	if tls.get('insecure'):
		print('🔓 已启用 insecure（跳过证书校验）')
	for note in notes:
		print(f'ℹ️  {note}')
	return 0


if __name__ == '__main__':
	sys.exit(main())
