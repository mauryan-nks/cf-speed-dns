#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cloudflare DNS 更新器
获取优选 IP 并更新 Cloudflare DNS 记录
"""

import json
import traceback
import time
import os
import sys
import ipaddress
import re

import requests

# API 配置
CF_API_TOKEN = os.environ.get("CF_API_TOKEN")
CF_ZONE_ID = os.environ.get("CF_ZONE_ID")
CF_DNS_NAME = os.environ.get("CF_DNS_NAME")
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN")

# 请求头
HEADERS = {
    'Authorization': f'Bearer {CF_API_TOKEN}',
    'Content-Type': 'application/json'
}

# 默认超时时间（秒）
DEFAULT_TIMEOUT = 30

# HTTP 会话
SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': 'cloudflare-dns-updater/1.0'
})


def parse_ipv4_addresses(text):
    """
    从返回内容中解析并验证 IPv4 地址

    Args:
        text: 返回的文本内容

    Returns:
        有效且去重后的 IPv4 地址列表
    """
    candidates = re.findall(
        r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])',
        text or ''
    )

    ip_addresses = []
    seen = set()

    for candidate in candidates:
        try:
            ip = str(ipaddress.IPv4Address(candidate))
        except ipaddress.AddressValueError:
            continue

        if ip not in seen:
            seen.add(ip)
            ip_addresses.append(ip)

    return ip_addresses


def get_cf_speed_test_ip(timeout=10, max_retries=5):
    """
    获取 Cloudflare 优选 IP

    Args:
        timeout: 单次请求超时时间
        max_retries: 最大重试次数

    Returns:
        优选 IP 字符串，失败返回 None
    """
    for attempt in range(max_retries):
        try:
            response = SESSION.get(
                'https://ip.164746.xyz/ipTop.html',
                timeout=timeout
            )

            if response.status_code == 200:
                ip_addresses = parse_ipv4_addresses(response.text)

                if ip_addresses:
                    return ','.join(ip_addresses)

                print(
                    f"获取优选 IP 失败 "
                    f"(尝试 {attempt + 1}/{max_retries}): "
                    "返回内容中没有有效 IPv4 地址"
                )
            else:
                print(
                    f"获取优选 IP 失败 "
                    f"(尝试 {attempt + 1}/{max_retries}): "
                    f"HTTP {response.status_code}"
                )

        except Exception as e:
            print(
                f"获取优选 IP 失败 "
                f"(尝试 {attempt + 1}/{max_retries}): {e}"
            )

            if attempt == max_retries - 1:
                traceback.print_exc()

        if attempt < max_retries - 1:
            time.sleep(min(2 ** attempt, 8))

    return None


def get_dns_records(name):
    """
    获取指定名称的 DNS 记录列表（仅 A 类型）

    Args:
        name: DNS 记录名称

    Returns:
        记录字典列表（包含 id 和 content），失败返回空列表
    """
    records = []

    url = (
        f'https://api.cloudflare.com/client/v4/'
        f'zones/{CF_ZONE_ID}/dns_records'
    )

    page = 1

    try:
        while True:
            params = {
                'type': 'A',
                'name': name,
                'page': page,
                'per_page': 100
            }

            response = SESSION.get(
                url,
                headers=HEADERS,
                params=params,
                timeout=DEFAULT_TIMEOUT
            )

            try:
                payload = response.json()
            except ValueError:
                print(
                    f'获取 DNS 记录失败: Cloudflare 返回无效 JSON，'
                    f'HTTP {response.status_code}: '
                    f'{response.text[:500]}'
                )
                return []

            if response.status_code == 200 and payload.get('success'):
                result = payload.get('result', [])

                for record in result:
                    # 只获取 A 类型记录，避免更新其他类型记录导致 400 错误
                    if (
                        record.get('name', '').rstrip('.').lower()
                        == name.rstrip('.').lower()
                        and record.get('type') == 'A'
                    ):
                        records.append({
                            'id': record['id'],
                            'content': record.get('content', '')
                        })

                result_info = payload.get('result_info', {})

                total_pages = int(
                    result_info.get('total_pages') or 1
                )

                if page >= total_pages:
                    break

                page += 1

            else:
                errors = payload.get('errors', [])

                if errors:
                    error_text = '; '.join(
                        f"{item.get('code', '')}: "
                        f"{item.get('message', '')}"
                        for item in errors
                    )
                else:
                    error_text = response.text

                print(f'获取 DNS 记录失败: {error_text}')
                return []

    except Exception as e:
        print(f'获取 DNS 记录异常: {e}')
        traceback.print_exc()

        return []

    # 保证每次获取后的记录顺序相对稳定
    records.sort(key=lambda record: record['id'])

    return records


def update_dns_record(record_info, name, cf_ip):
    """
    更新 DNS 记录

    Args:
        record_info: DNS 记录字典，包含 id 和 content
        name: DNS 记录名称
        cf_ip: 新的 IP 地址

    Returns:
        操作结果字符串
    """
    record_id = record_info['id']
    current_ip = record_info.get('content', '')

    # 如果 IP 相同则跳过更新
    if current_ip == cf_ip:
        current_time = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime()
        )

        print(
            f"cf_dns_change skip: "
            f"---- Time: {current_time} ---- "
            f"ip：{cf_ip} (已是最新)"
        )

        return f"ip:{cf_ip} 解析 {name} 跳过 (已是最新)"

    url = (
        f'https://api.cloudflare.com/client/v4/'
        f'zones/{CF_ZONE_ID}/dns_records/{record_id}'
    )

    # 使用 PATCH 只修改 IP，避免覆盖 TTL、代理状态等其他记录配置
    data = {
        'content': cf_ip
    }

    try:
        response = SESSION.patch(
            url,
            headers=HEADERS,
            json=data,
            timeout=DEFAULT_TIMEOUT
        )

        current_time = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime()
        )

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.status_code == 200 and payload.get('success'):
            print(
                f"cf_dns_change success: "
                f"---- Time: {current_time} ---- "
                f"ip：{cf_ip}"
            )

            return f"ip:{cf_ip} 解析 {name} 成功"

        errors = payload.get('errors', [])

        if errors:
            error_text = '; '.join(
                f"{item.get('code', '')}: "
                f"{item.get('message', '')}"
                for item in errors
            )
        else:
            error_text = response.text

        print(
            f"cf_dns_change ERROR: "
            f"---- Time: {current_time} ---- "
            f"MESSAGE: {error_text}"
        )

        return f"ip:{cf_ip} 解析 {name} 失败"

    except Exception as e:
        traceback.print_exc()

        current_time = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime()
        )

        print(
            f"cf_dns_change ERROR: "
            f"---- Time: {current_time} ---- "
            f"MESSAGE: {e}"
        )

        return f"ip:{cf_ip} 解析 {name} 失败"


def build_update_plan(dns_records, ip_addresses):
    """
    构建 DNS 更新计划

    优先保留已经存在的正确 IP，
    避免因为 Cloudflare 返回记录顺序变化而产生无意义更新。

    Args:
        dns_records: Cloudflare DNS 记录列表
        ip_addresses: 优选 IP 列表

    Returns:
        (DNS记录, IP) 元组列表
    """
    remaining_records = list(dns_records)
    update_plan = []
    unmatched_ips = []

    # 先检查优选 IP 是否已经存在于 DNS 记录中
    for ip_address in ip_addresses:
        matched_index = None

        for index, record in enumerate(remaining_records):
            if record.get('content') == ip_address:
                matched_index = index
                break

        if matched_index is None:
            unmatched_ips.append(ip_address)
        else:
            record = remaining_records.pop(matched_index)
            update_plan.append((record, ip_address))

    # 将还不存在的 IP 分配给剩余 DNS 记录
    for record, ip_address in zip(
        remaining_records,
        unmatched_ips
    ):
        update_plan.append((record, ip_address))

    return update_plan


def push_plus(content):
    """
    发送 PushPlus 消息推送

    Args:
        content: 消息内容
    """
    if not PUSHPLUS_TOKEN:
        print("PUSHPLUS_TOKEN 未设置，跳过消息推送")
        return

    url = 'https://www.pushplus.plus/send'

    data = {
        "token": PUSHPLUS_TOKEN,
        "title": "IP优选DNSCF推送",
        "content": content,
        "template": "markdown",
        "channel": "wechat"
    }

    try:
        body = json.dumps(
            data,
            ensure_ascii=False
        ).encode(encoding='utf-8')

        headers = {
            'Content-Type': 'application/json'
        }

        response = SESSION.post(
            url,
            data=body,
            headers=headers,
            timeout=DEFAULT_TIMEOUT
        )

        if response.status_code != 200:
            print(
                f"消息推送失败: "
                f"HTTP {response.status_code} "
                f"{response.text}"
            )
            return

        try:
            payload = response.json()

            if (
                isinstance(payload, dict)
                and payload.get('code') not in (None, 200)
            ):
                print(
                    f"消息推送失败: "
                    f"{json.dumps(payload, ensure_ascii=False)}"
                )

        except ValueError:
            pass

    except Exception as e:
        print(f"消息推送失败: {e}")


def main():
    """主函数"""
    # 检查必要的环境变量
    if not all([CF_API_TOKEN, CF_ZONE_ID, CF_DNS_NAME]):
        print(
            "错误: 缺少必要的环境变量 "
            "(CF_API_TOKEN, CF_ZONE_ID, CF_DNS_NAME)"
        )
        return 1

    # 获取最新优选 IP
    ip_addresses_str = get_cf_speed_test_ip()

    if not ip_addresses_str:
        print("错误: 无法获取优选 IP")
        return 1

    ip_addresses = [
        ip.strip()
        for ip in ip_addresses_str.split(',')
        if ip.strip()
    ]

    if not ip_addresses:
        print("错误: 未解析到有效 IP 地址")
        return 1

    print(
        "获取到优选 IP: "
        + ', '.join(ip_addresses)
    )

    # 获取 DNS 记录
    dns_records = get_dns_records(CF_DNS_NAME)

    if not dns_records:
        print(
            f"错误: 未找到 {CF_DNS_NAME} "
            "的 DNS 记录"
        )
        return 1

    print(
        f"找到 {len(dns_records)} 个 "
        f"{CF_DNS_NAME} A 记录"
    )

    # 检查记录数量是否足够
    if len(ip_addresses) > len(dns_records):
        print(
            f"警告: IP 数量({len(ip_addresses)})"
            f"超过 DNS 记录数量({len(dns_records)})，"
            f"只更新前 {len(dns_records)} 个"
        )

        ip_addresses = ip_addresses[
            :len(dns_records)
        ]

    elif len(ip_addresses) < len(dns_records):
        print(
            f"警告: IP 数量({len(ip_addresses)})"
            f"少于 DNS 记录数量({len(dns_records)})，"
            "多余的 DNS 记录保持不变"
        )

    # 根据当前 DNS 状态构建更新计划
    update_plan = build_update_plan(
        dns_records,
        ip_addresses
    )

    # 更新 DNS 记录
    push_plus_content = []
    has_error = False

    for record_info, ip_address in update_plan:
        dns = update_dns_record(
            record_info,
            CF_DNS_NAME,
            ip_address
        )

        push_plus_content.append(dns)

        if '失败' in dns:
            has_error = True

    # 发送推送
    if push_plus_content:
        push_plus(
            '\n'.join(push_plus_content)
        )

    return 1 if has_error else 0


if __name__ == '__main__':
    sys.exit(main())
