#!/usr/bin/env python3
"""
AnyRouter 自动签到脚本（GitHub Actions 专用）
"""

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime

if hasattr(sys.stdout, 'reconfigure'):
	sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
	sys.stderr.reconfigure(line_buffering=True)

import httpx
from cloakbrowser import launch_async

from utils.browser import (
	BrowserLoginResult,
	has_session_cookie,
	is_logged_in,
	launch_login_context,
	load_browser_login_settings,
	login_with_email_form,
	navigate_login_page,
	prepare_browser_page,
	save_login_screenshot,
	take_pending_screenshots,
	verify_browser_login,
	wait_for_waf_ready,
)
from utils.config import AccountConfig, AppConfig, load_accounts_config
from utils import log
from utils.debug import is_debug_enabled
from utils.notify import notify
from utils.proxy import get_playwright_proxy, get_proxy_server

BALANCE_HASH_FILE = 'balance_hash.txt'


def load_balance_hash():
	"""加载余额hash"""
	try:
		if os.path.exists(BALANCE_HASH_FILE):
			with open(BALANCE_HASH_FILE, 'r', encoding='utf-8') as f:
				return f.read().strip()
	except Exception:  # nosec B110
		pass
	return None


def save_balance_hash(balance_hash):
	"""保存余额hash"""
	try:
		with open(BALANCE_HASH_FILE, 'w', encoding='utf-8') as f:
			f.write(balance_hash)
	except Exception as e:
		log.warn(f'保存余额指纹失败：{e}')


def generate_balance_hash(balances):
	"""生成余额数据的hash"""
	simple_balances = (
		{k: {'quota': v.get('quota'), 'used': v.get('used')} for k, v in balances.items()} if balances else {}
	)
	balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
	return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]


def parse_cookies(cookies_data):
	"""解析 cookies 数据"""
	if isinstance(cookies_data, dict):
		return cookies_data

	if isinstance(cookies_data, str):
		cookies_dict = {}
		for cookie in cookies_data.split(';'):
			if '=' in cookie:
				key, value = cookie.strip().split('=', 1)
				cookies_dict[key] = value
		return cookies_dict
	return {}


async def get_waf_cookies_with_browser(
	account_name: str,
	login_url: str,
	required_cookies: list[str],
	*,
	use_proxy: bool = False,
):
	"""使用浏览器获取 WAF cookies"""
	log.step(f'[{account_name}] 启动浏览器获取 WAF cookies...')

	launch_kwargs: dict = {'headless': True}
	proxy = get_playwright_proxy(use_proxy=use_proxy)
	if proxy:
		launch_kwargs['proxy'] = proxy
	browser = await launch_async(**launch_kwargs)

	try:
		page = await browser.new_page()
		await prepare_browser_page(page)
		log.step(f'[{account_name}] 访问登录页获取初始 cookies...')

		await page.goto(login_url, wait_until='domcontentloaded')
		await wait_for_waf_ready(page)

		cookies = await page.context.cookies()

		waf_cookies = {}
		for cookie in cookies:
			cookie_name = cookie.get('name')
			cookie_value = cookie.get('value')
			if cookie_name in required_cookies and cookie_value is not None:
				waf_cookies[cookie_name] = cookie_value

		log.info(f'[{account_name}] 获得 {len(waf_cookies)} 个 WAF cookies')

		missing_cookies = [c for c in required_cookies if c not in waf_cookies]

		if missing_cookies:
			log.fail(f'[{account_name}] 缺少 WAF cookies：{missing_cookies}')
			await browser.close()
			return None

		log.ok(f'[{account_name}] WAF cookies 获取成功')
		await browser.close()
		return waf_cookies

	except Exception as e:
		log.fail(f'[{account_name}] 获取 WAF cookies 出错：{e}')
		await browser.close()
		return None


async def login_with_credentials(
	account_name: str,
	provider_config,
	provider_name: str,
	email: str,
	password: str,
) -> BrowserLoginResult | None:
	"""使用邮箱密码通过浏览器登录，返回 cookies 与拦截到的 api user id。"""
	log.step(f'[{account_name}] 使用邮箱密码登录...')

	login_url = f'{provider_config.domain}{provider_config.login_path}'
	settings = load_browser_login_settings(
		account_name,
		provider_name,
		persist_profile=provider_config.persist_profile,
	)
	timeout_ms = settings.wait_timeout_ms

	log.debug(
		f'[{account_name}] 浏览器 profile={settings.profile_dir}, '
		f'persist={settings.persist_profile}, headless={settings.headless}, '
		f'humanize={settings.humanize}, timeout={timeout_ms}ms'
	)

	log.proxy(f'[{account_name}] 平台 {provider_name} 代理：{"已启用" if provider_config.use_proxy else "未启用"}')

	try:
		context = await launch_login_context(settings, use_proxy=provider_config.use_proxy)
	except Exception as e:
		log.fail(f'[{account_name}] 浏览器启动失败：{e}')
		return None

	page = None
	try:
		page = await context.new_page()
		await prepare_browser_page(page)
		await navigate_login_page(
			page,
			login_url,
			timeout_ms,
			provider=provider_name,
			account_name=account_name,
		)

		if not await is_logged_in(page):
			if await has_session_cookie(page):
				log.warn(f'[{account_name}] 登录页残留过期会话 cookie，强制走邮箱登录')
			await save_login_screenshot(page, provider_name, account_name, 'before-email-login')
			await login_with_email_form(
				page,
				email,
				password,
				timeout_ms,
				provider=provider_name,
				account_name=account_name,
			)
		else:
			log.info(f'[{account_name}] 浏览器 profile 已处于登录状态')

		console_url = f'{provider_config.domain}/console'
		user_profile = await verify_browser_login(page, console_url, timeout_ms)
		if not user_profile:
			cookies = await context.cookies()
			cookie_names = [c.get('name') for c in cookies if c.get('name')]
			log.fail(f'[{account_name}] 登录失败：/api/user/self 未通过验证')
			log.debug(f'[{account_name}] 当前 URL：{page.url}')
			log.debug(f'[{account_name}] 已获得 cookies：{cookie_names}')
			await save_login_screenshot(page, provider_name, account_name, 'not-authenticated')
			await context.close()
			return None

		cookies = await context.cookies()
		all_cookies = {
			cookie.get('name'): cookie.get('value') for cookie in cookies if cookie.get('name') and cookie.get('value')
		}
		api_user = str(user_profile['id']) if user_profile.get('id') is not None else None

		success_msg = f'[{account_name}] 登录成功，获得 {len(all_cookies)} 个 cookies'
		if is_debug_enabled() and api_user:
			success_msg += f', api_user={api_user}'
		log.ok(success_msg)
		await context.close()
		return BrowserLoginResult(cookies=all_cookies, api_user=api_user)

	except Exception as e:
		log.fail(f'[{account_name}] 登录过程出错：{e}')
		if page is not None:
			await save_login_screenshot(page, provider_name, account_name, 'login-error')
		await context.close()
		return None


def get_user_info(client, headers, user_info_url: str):
	"""获取用户信息"""
	try:
		response = client.get(user_info_url, headers=headers, timeout=30)

		if response.status_code == 200:
			data = response.json()
			if data.get('success'):
				user_data = data.get('data', {})
				quota = round(user_data.get('quota', 0) / 500000, 2)
				used_quota = round(user_data.get('used_quota', 0) / 500000, 2)
				return {
					'success': True,
					'quota': quota,
					'used_quota': used_quota,
					'display': f'💰 当前余额：${quota}，已使用：${used_quota}',
				}
		return {'success': False, 'error': f'获取用户信息失败：HTTP {response.status_code}'}
	except Exception as e:
		return {'success': False, 'error': f'获取用户信息失败：{str(e)[:50]}...'}


async def prepare_cookies(account_name: str, provider_config, user_cookies: dict) -> dict | None:
	"""准备请求所需的 cookies（可能包含 WAF cookies）"""
	waf_cookies = {}

	if provider_config.needs_waf_cookies():
		login_url = f'{provider_config.domain}{provider_config.login_path}'
		waf_cookies = await get_waf_cookies_with_browser(
			account_name,
			login_url,
			provider_config.waf_cookie_names,
			use_proxy=provider_config.use_proxy,
		)
		if not waf_cookies:
			log.fail(f'[{account_name}] 无法获取 WAF cookies')
			return None
	else:
		log.info(f'[{account_name}] 无需绕过 WAF，直接使用用户 cookies')

	return {**waf_cookies, **user_cookies}


def execute_check_in(client, account_name: str, provider_config, headers: dict):
	"""执行签到请求"""
	log.network(f'[{account_name}] 执行签到请求')

	checkin_headers = headers.copy()
	checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})

	sign_in_url = f'{provider_config.domain}{provider_config.sign_in_path}'
	response = client.post(sign_in_url, headers=checkin_headers, timeout=30)

	log.response(f'[{account_name}] 响应状态码 {response.status_code}')

	if response.status_code == 200:
		try:
			result = response.json()
			if result.get('ret') == 1 or result.get('code') == 0 or result.get('success'):
				log.ok(f'[{account_name}] 签到成功 🎉')
				return True
			else:
				error_msg = result.get('msg', result.get('message', 'Unknown error'))
				already_checked_keywords = ['已经签到', '已签到', '重复签到', 'already checked', 'already signed']
				if any(keyword in error_msg.lower() for keyword in already_checked_keywords):
					log.ok(f'[{account_name}] 今日已签到过')
					return True
				log.fail(f'[{account_name}] 签到失败：{error_msg}')
				return False
		except json.JSONDecodeError:
			if 'success' in response.text.lower():
				log.ok(f'[{account_name}] 签到成功 🎉')
				return True
			else:
				log.fail(f'[{account_name}] 签到失败：响应格式无效')
				return False
	else:
		log.fail(f'[{account_name}] 签到失败：HTTP {response.status_code}')
		return False


def format_check_in_notification(detail: dict) -> str:
	"""格式化签到通知消息"""
	lines = [
		f'🎯 {detail["name"]}',
		'  ━━━━━━━━━━━━━━━━━━━━',
		'  📥 签到前',
		f'     余额: ${detail["before_quota"]:.2f}  |  累计消耗: ${detail["before_used"]:.2f}',
		'  📤 签到后',
		f'     余额: ${detail["after_quota"]:.2f}  |  累计消耗: ${detail["after_used"]:.2f}',
	]

	has_reward = detail['check_in_reward'] != 0
	has_usage = detail['usage_increase'] != 0

	if has_reward or has_usage:
		lines.append('  ━━━━━━━━━━━━━━━━━━━━')

		if not has_reward and has_usage:
			lines.append('  ✅ 今日已签到（期间有使用）')

		if has_reward:
			lines.append(f'  🎁 签到获得: +${detail["check_in_reward"]:.2f}')

		if has_usage:
			lines.append(f'  🔥 期间消耗: ${detail["usage_increase"]:.2f}')

		if detail['balance_change'] != 0:
			change_symbol = '+' if detail['balance_change'] > 0 else ''
			lines.append(f'  💰 余额变化: {change_symbol}${detail["balance_change"]:.2f}')
	else:
		lines.extend(['  ━━━━━━━━━━━━━━━━━━━━', '  ✅ 今日已签到，无变化'])

	return '\n'.join(lines)


async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig):
	"""为单个账号执行签到操作"""
	account_name = account.get_display_name(account_index)
	log.section(f'👤 账号 {account_index + 1}：{account_name}')

	provider_config = app_config.get_provider(account.provider)
	if not provider_config:
		log.fail(f'[{account_name}] 配置中找不到平台 "{account.provider}"')
		return False, None, None

	log.info(f'[{account_name}] 平台：{account.provider}（{provider_config.domain}）')

	# 邮箱密码优先
	all_cookies = None
	resolved_api_user: str | None = None
	auth_method = None
	if account.has_login_credentials():
		log.info(f'[{account_name}] 优先尝试邮箱密码登录...')
		assert account.email is not None and account.password is not None
		login_result = await login_with_credentials(
			account_name,
			provider_config,
			account.provider,
			account.email,
			account.password,
		)
		if login_result:
			all_cookies = login_result.cookies
			resolved_api_user = login_result.api_user
			auth_method = 'email/password'
		else:
			log.fail(f'[{account_name}] 邮箱密码登录失败，不会回退使用过期的会话 cookies')
			return False, None, None
	else:
		user_cookies = parse_cookies(account.cookies)
		if not user_cookies:
			log.fail(f'[{account_name}] 账号配置格式无效')
			return False, None, None
		all_cookies = await prepare_cookies(account_name, provider_config, user_cookies)
		auth_method = 'session cookies'

	if not all_cookies:
		return False, None, None

	log.auth(f'[{account_name}] 认证方式：{auth_method}')

	return run_check_in_requests(
		all_cookies,
		account,
		account_name,
		provider_config,
		api_user_override=resolved_api_user,
		use_proxy=provider_config.use_proxy,
	)


def run_check_in_requests(
	all_cookies: dict,
	account: AccountConfig,
	account_name: str,
	provider_config,
	*,
	api_user_override: str | None = None,
	use_proxy: bool = False,
) -> tuple[bool, dict | None, dict | None]:
	"""执行 HTTP 签到请求（同步，避免在 async 上下文中使用阻塞 httpx）。"""
	try:
		client_kwargs: dict = {'http2': True, 'timeout': 30.0}
		proxy_url = get_proxy_server(use_proxy=use_proxy)
		if proxy_url:
			client_kwargs['proxy'] = proxy_url
			if is_debug_enabled():
				log.proxy(f'[{account_name}] HTTP 客户端已走代理：{proxy_url}')
			else:
				log.proxy(f'[{account_name}] HTTP 客户端已走代理')

		with httpx.Client(**client_kwargs) as client:
			client.cookies.update(all_cookies)

			headers = {
				'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
				'Accept': 'application/json, text/plain, */*',
				'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
				'Accept-Encoding': 'gzip, deflate, br, zstd',
				'Referer': provider_config.domain,
				'Origin': provider_config.domain,
				'Connection': 'keep-alive',
				'Sec-Fetch-Dest': 'empty',
				'Sec-Fetch-Mode': 'cors',
				'Sec-Fetch-Site': 'same-origin',
			}

			api_user = api_user_override or account.api_user
			if api_user:
				headers[provider_config.api_user_key] = api_user

			user_info_url = f'{provider_config.domain}{provider_config.user_info_path}'
			user_info_before = get_user_info(client, headers, user_info_url)
			if user_info_before and user_info_before.get('success'):
				log.say(f'   签到前 {user_info_before["display"]}')
			elif user_info_before:
				log.warn(f'[{account_name}] {user_info_before.get("error", "未知错误")}')

			if provider_config.needs_manual_check_in():
				success = execute_check_in(client, account_name, provider_config, headers)
				user_info_after = get_user_info(client, headers, user_info_url)
				return success, user_info_before, user_info_after

			user_info_after = get_user_info(client, headers, user_info_url)
			if user_info_after and user_info_after.get('success'):
				log.ok(f'[{account_name}] 已自动完成签到（查询用户信息时触发）')
				return True, user_info_before, user_info_after
			error = user_info_after.get('error', '未知错误') if user_info_after else '未知错误'
			log.fail(f'[{account_name}] 自动签到失败：{error}')
			return False, user_info_before, user_info_after

	except Exception as e:
		log.fail(f'[{account_name}] 签到过程出错：{str(e)[:50]}...')
		return False, None, None


async def main():
	"""主函数"""
	log.banner('🚀 AnyRouter 多账号自动签到')
	log.say(f'🕐 执行时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

	if is_debug_enabled():
		log.debug('调试模式已开启（截图 + 详细日志）')
	else:
		log.info('调试模式未开启（设置 DEBUG_MODE=true 可开启截图与详细日志）')

	proxy_server = os.getenv('CHECKIN_PROXY_URL', '').strip()
	if proxy_server:
		exit_ip = os.getenv('CHECKIN_PROXY_EXIT', '').strip()
		suffix = f'，出口 IP：{exit_ip}' if exit_ip else ''
		log.proxy(f'代理已就绪{suffix}（仅对开启 use_proxy 的平台生效）')
	else:
		log.proxy('未检测到代理')

	app_config = AppConfig.load_from_env()
	log.info(f'已加载 {len(app_config.providers)} 个平台配置')
	for provider_name, provider in sorted(app_config.providers.items()):
		log.debug(f'平台 "{provider_name}"：use_proxy={provider.use_proxy}')

	accounts = load_accounts_config()
	if not accounts:
		error_msg = '❌ 无法加载账号配置，程序退出（请检查 ANYROUTER_ACCOUNTS Secret）'
		log.say(error_msg)
		notify.push_message('AnyRouter Check-in Alert', error_msg, msg_type='text')
		sys.exit(1)

	log.info(f'共发现 {len(accounts)} 个账号配置')

	# 要求走代理的账号在代理未就绪时一律不签到（不回退直连，避免暴露 runner IP）
	if not proxy_server:
		proxy_needed = []
		for idx, acc in enumerate(accounts):
			prov = app_config.get_provider(acc.provider)
			if prov and prov.use_proxy:
				proxy_needed.append(acc.get_display_name(idx))
		if proxy_needed:
			error_msg = f'❌ 代理未就绪，以下账号要求走代理，已中止签到：{", ".join(proxy_needed)}'
			log.say(error_msg)
			notify.push_message('AnyRouter Check-in Alert', error_msg, msg_type='text')
			sys.exit(1)

	last_balance_hash = load_balance_hash()

	success_count = 0
	total_count = len(accounts)
	notification_content = []
	current_balances = {}
	account_check_in_details = {}
	need_notify = False
	balance_changed = False
	account_results = []

	for i, account in enumerate(accounts):
		account_key = f'account_{i + 1}'
		try:
			success, user_info_before, user_info_after = await check_in_account(account, i, app_config)
			account_results.append((account.get_display_name(i), success, user_info_after))
			if success:
				success_count += 1

			should_notify_this_account = False

			if not success:
				should_notify_this_account = True
				need_notify = True
				account_name = account.get_display_name(i)
				log.notify(f'[{account_name}] 签到失败，将发送通知')

			if user_info_after and user_info_after.get('success'):
				current_quota = user_info_after['quota']
				current_used = user_info_after['used_quota']
				current_balances[account_key] = {'quota': current_quota, 'used': current_used}

				if user_info_before and user_info_before.get('success'):
					before_quota = user_info_before['quota']
					before_used = user_info_before['used_quota']
					after_quota = user_info_after['quota']
					after_used = user_info_after['used_quota']

					total_before = before_quota + before_used
					total_after = after_quota + after_used

					check_in_reward = total_after - total_before
					usage_increase = after_used - before_used
					balance_change = after_quota - before_quota

					account_check_in_details[account_key] = {
						'name': account.get_display_name(i),
						'before_quota': before_quota,
						'before_used': before_used,
						'after_quota': after_quota,
						'after_used': after_used,
						'check_in_reward': check_in_reward,
						'usage_increase': usage_increase,
						'balance_change': balance_change,
						'success': success,
					}

			if should_notify_this_account:
				account_name = account.get_display_name(i)
				status = '✅' if success else '❌'
				account_result = f'{status} {account_name}'
				if user_info_after and user_info_after.get('success'):
					account_result += f'\n{user_info_after["display"]}'
				elif user_info_after:
					account_result += f'\n{user_info_after.get("error", "未知错误")}'
				notification_content.append(account_result)

		except Exception as e:
			account_name = account.get_display_name(i)
			log.fail(f'[{account_name}] 处理异常：{e}')
			account_results.append((account_name, False, None))
			need_notify = True
			notification_content.append(f'❌ {account_name} 异常：{str(e)[:50]}...')

	log.section('📊 签到汇总')
	for result_name, result_success, result_info in account_results:
		line = f'{"✅" if result_success else "❌"} {result_name}'
		if result_info and result_info.get('success'):
			line += f'  💰 ${result_info["quota"]}（已用 ${result_info["used_quota"]}）'
		log.say(line)
	log.say(f'🏁 成功 {success_count}/{total_count}，失败 {total_count - success_count}/{total_count}')
	log.say()

	current_balance_hash = generate_balance_hash(current_balances) if current_balances else None
	if current_balance_hash:
		if last_balance_hash is None:
			balance_changed = True
			need_notify = True
			log.notify('首次运行，将发送包含当前余额的通知')
		elif current_balance_hash != last_balance_hash:
			balance_changed = True
			need_notify = True
			log.notify('检测到余额变化，将发送通知')
		else:
			log.info('余额无变化')

	if balance_changed:
		for i, account in enumerate(accounts):
			account_key = f'account_{i + 1}'
			if account_key in account_check_in_details:
				detail = account_check_in_details[account_key]
				account_name = detail['name']
				account_result = format_check_in_notification(detail)
				if not any(account_name in item for item in notification_content):
					notification_content.append(account_result)

	if current_balance_hash:
		save_balance_hash(current_balance_hash)

	if need_notify and notification_content:
		summary = [
			'📊 签到结果统计：',
			f'✅ 成功：{success_count}/{total_count}',
			f'❌ 失败：{total_count - success_count}/{total_count}',
		]

		if success_count == total_count:
			summary.append('🎉 所有账号签到成功！')
		elif success_count > 0:
			summary.append('⚠️ 部分账号签到成功')
		else:
			summary.append('🚨 所有账号签到失败')

		time_info = f'🕐 执行时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'

		notify_content = '\n\n'.join([time_info, '\n'.join(notification_content), '\n'.join(summary)])
		screenshot_paths = take_pending_screenshots() if is_debug_enabled() else []
		if screenshot_paths:
			github_run_id = os.getenv('GITHUB_RUN_ID', '').strip()
			github_repo = os.getenv('GITHUB_REPOSITORY', '').strip()
			screenshot_hint = f'📸 已保存 {len(screenshot_paths)} 张调试截图'
			if github_run_id and github_repo:
				run_url = f'https://github.com/{github_repo}/actions/runs/{github_run_id}'
				screenshot_hint += f'，请在运行页下载 artifact `checkin-screenshots-{github_run_id}`：{run_url}'
			else:
				screenshot_hint += '到 `checkin_screenshots/`'
			notify_content += f'\n\n{screenshot_hint}'

		log.section('📣 推送通知')
		log.say(notify_content)
		log.say()
		notify.push_message('AnyRouter Check-in Alert', notify_content, msg_type='text')
		log.notify('已因签到失败或余额变化发送通知')
	else:
		log.ok('全部账号签到成功且余额无变化，已跳过通知')

	sys.exit(0 if success_count > 0 else 1)


def run_main():
	"""运行主函数的包装函数"""
	try:
		asyncio.run(main())
	except KeyboardInterrupt:
		log.say()
		log.warn('程序被用户中断')
		sys.exit(1)
	except Exception as e:
		log.say()
		log.fail(f'程序运行出错：{e}')
		sys.exit(1)


if __name__ == '__main__':
	run_main()
