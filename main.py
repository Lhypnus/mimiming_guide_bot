import os
import discord
from discord.ext import commands
from discord import app_commands
from discord.app_commands.translator import Translator, TranslationContext, locale_str
from dotenv import load_dotenv
import notion_client
import json
import requests
import datetime
import re
from collections import defaultdict
import time

# .env 파일에서 환경 변수 로드
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
NOTION_API_KEY = os.getenv('NOTION_API_KEY')
NOTION_BUYER_DATABASE_ID = os.getenv('NOTION_BUYER_DATABASE_ID')
VERIFICATION_CHANNEL_ID = os.getenv('VERIFICATION_CHANNEL_ID')
LOG_WEBHOOK_URL = os.getenv('LOG_WEBHOOK_URL')

# 다국어 지원을 위한 설정
LOCALES = {}
for lang in ["ko", "en", "ja", "zh-CN", "zh-TW"]:
    try:
        with open(f"locales/{lang}.json", "r", encoding="utf-8") as f:
            LOCALES[lang] = json.load(f)
    except FileNotFoundError:
        print(f"Warning: Locale file for '{lang}' not found.")
    except json.JSONDecodeError:
        print(f"Warning: Could not decode locale file for '{lang}'. Check for syntax errors.")

# 번역 키를 기반으로 번역된 문자열을 가져오는 함수
def get_translation(key: str, locale_str: str):
    """
    주어진 키와 로케일 문자열에 대한 번역문을 반환합니다.
    1. 로케일 문자열과 정확히 일치하는 언어 파일(예: 'zh-CN')을 찾습니다.
    2. 없으면, 언어 코드 부분(예: 'en-US' -> 'en')과 일치하는 파일을 찾습니다.
    3. 그래도 없으면, 기본 언어인 영어('en')를 사용합니다.
    """
    # 1. 로케일과 정확히 일치하는 번역본 찾기
    if locale_str in LOCALES:
        lang = locale_str
    # 2. 언어 코드 부분만으로 일치하는 번역본 찾기
    else:
        lang_part = locale_str.split('-')[0]
        if lang_part in LOCALES:
            lang = lang_part
        # 3. 기본 언어(영어)로 대체
        else:
            lang = 'en'
            
    # 번역된 문자열 반환 (최종적으로 영어 -> 키 자체로 폴백)
    en_locale = LOCALES.get('en', {})
    target_locale = LOCALES.get(lang, en_locale)
    return target_locale.get(key, en_locale.get(key, key))

# 커맨드 번역을 처리하는 클래스
class MyTranslator(Translator):
    async def translate(self, string: locale_str, locale: discord.Locale, context: TranslationContext) -> str | None:
        # 명령어 이름, 그룹 이름, 파라미터 이름 등은 번역하지 않도록 설정
        # None을 반환하면 discord.py가 기본값을 사용합니다.
        if context.location in (
            app_commands.TranslationContextLocation.command_name,
            app_commands.TranslationContextLocation.group_name,
            app_commands.TranslationContextLocation.parameter_name,
        ):
            return None
            
        # 그 외(설명 등)는 번역을 시도합니다.
        return get_translation(string.message, str(locale))

# 봇의 권한(Intents) 설정
intents = discord.Intents.default()
intents.message_content = True # 메시지 내용을 읽기 위한 권한
intents.members = True # 멤버 관리를 위한 권한

# Translator를 포함한 커스텀 봇 클래스
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='/', intents=intents)

    async def setup_hook(self):
        # 번역기 설정
        translator = MyTranslator()
        await self.tree.set_translator(translator)
        # 슬래시 커맨드 동기화
        try:
            synced = await self.tree.sync()
            print(f"{len(synced)}개의 커맨드를 동기화했습니다.")
        except Exception as e:
            print(f"커맨드 동기화 중 오류 발생: {e}")

bot = MyBot()


# Notion 클라이언트 초기화
notion = None
if NOTION_API_KEY:
    notion = notion_client.AsyncClient(auth=NOTION_API_KEY)



@bot.event
async def on_ready():
    """봇이 준비되었을 때 실행되는 이벤트"""
    print(f'{bot.user.name} 봇이 성공적으로 로그인했습니다!')
    print(f'봇 ID: {bot.user.id}')
    print('------')





def send_verification_log(user, code, success=True, reason=None):
    if not LOG_WEBHOOK_URL:
        print("인증 로그 웹훅 URL이 설정되지 않았습니다.")
        return
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if success:
        emoji = "✅"
        code_str = f"`{code}`"
        reason_str = ""
    else:
        emoji = "❗"
        code_str = f"**`{code}`**"
        reason_str = f" | {reason}" if reason else ""
    content = f"{emoji} {user.mention} (`{user.id}`) | {code_str} | {now}{reason_str}"
    data = {"content": content}
    try:
        requests.post(LOG_WEBHOOK_URL, json=data)
    except Exception as e:
        print(f"웹훅 전송 실패: {e}")

# 사용자별 시도 횟수 제한을 위한 변수
user_attempts = defaultdict(list)
MAX_ATTEMPTS = 10  # 1시간당 최대 시도 횟수
ATTEMPT_WINDOW = 3600  # 1시간 (초 단위)

# 코드 형식 검증 함수
def is_valid_code_format(code: str) -> bool:
    # #A1B2C 형식만 허용 (# + 영어/숫자 5자리)
    return bool(re.match(r'^#[A-Za-z0-9]{5}$', code))

@bot.tree.command(
    name="verify",
    description=locale_str("Get the 'Buyer' role by entering your skin code.")
)
@commands.cooldown(1, 30, commands.BucketType.user)  # 30초당 1번만 사용 가능
async def verify(interaction: discord.Interaction, code: str):
    """구매자 역할을 부여하는 커맨드"""
    locale = str(interaction.locale)
    await interaction.response.defer(ephemeral=True)

    # 코드 형식 검증
    if not is_valid_code_format(code):
        error_message = get_translation("verify_invalid_format", locale)
        await interaction.followup.send(error_message, ephemeral=True)
        return

    # 사용자별 시도 횟수 제한 확인
    user_id = interaction.user.id
    current_time = time.time()
    
    # 1시간 이내의 시도만 유지
    user_attempts[user_id] = [t for t in user_attempts[user_id] if current_time - t < ATTEMPT_WINDOW]
    
    if len(user_attempts[user_id]) >= MAX_ATTEMPTS:
        error_message = get_translation("verify_too_many_attempts", locale)
        await interaction.followup.send(error_message, ephemeral=True)
        return
        
    user_attempts[user_id].append(current_time)

    # 명령어가 서버에서 사용되었는지 확인
    if not interaction.guild:
        error_message = get_translation("verify_dm_error", locale)
        await interaction.followup.send(error_message, ephemeral=True)
        return

    # 지정된 채널에서 사용되었는지 확인 (VERIFICATION_CHANNEL_ID가 설정된 경우에만)
    if VERIFICATION_CHANNEL_ID and str(interaction.channel.id) != VERIFICATION_CHANNEL_ID:
        channel_mention = f"<#{VERIFICATION_CHANNEL_ID}>"
        error_message = get_translation("verify_wrong_channel_error", locale).format(channel_mention=channel_mention)
        await interaction.followup.send(error_message, ephemeral=True)
        return

    # 0. Notion 클라이언트가 설정되었는지 확인
    if not notion or not NOTION_BUYER_DATABASE_ID:
        print("Notion API Key or Database ID is not configured.")
        error_message = get_translation("verify_notion_api_error", locale)
        await interaction.followup.send(error_message)
        return

    # 1. 역할 이름 정의 및 서버에서 역할 찾기
    # 서버에 실제 생성해야 하는 역할의 이름은 "✅ Buyer" 하나입니다.
    CANONICAL_BUYER_ROLE_NAME = "✅ Buyer"
    buyer_role = discord.utils.get(interaction.guild.roles, name=CANONICAL_BUYER_ROLE_NAME)

    # 번역된 역할 이름은 사용자에게 보여줄 메시지에만 사용됩니다.
    translated_role_name = get_translation("role_name_buyer", locale)

    if not buyer_role:
        error_message = get_translation("verify_role_not_found_error", locale).format(role_name=translated_role_name)
        await interaction.followup.send(error_message, ephemeral=True)
        return

    # 2. 사용자가 이미 역할을 가지고 있는지 확인
    if buyer_role in interaction.user.roles:
        message = get_translation("verify_already_verified", locale).format(role_name=translated_role_name)
        await interaction.followup.send(message, ephemeral=True)
        return

    # 3. Notion DB에서 코드 검색
    try:
        query_result = await notion.databases.query(
            database_id=NOTION_BUYER_DATABASE_ID,
            filter={
                "property": "본계정",
                "title": {
                    "equals": code
                }
            }
        )

        if not query_result["results"]:
            message = get_translation("verify_code_not_found", locale).format(code=code)
            await interaction.followup.send(message, ephemeral=True)
            send_verification_log(interaction.user, code, success=False, reason="코드 없음")
            return

        # 4. 코드가 존재하면, 사용 여부 확인 후 역할 부여 및 DB 업데이트
        page_data = query_result["results"][0]
        page_id = page_data["id"]
        
        # '✅ Buyer 역할' 체크박스 속성 확인
        buyer_role_property = page_data.get("properties", {}).get("✅ Buyer 역할", {})
        if buyer_role_property.get("checkbox"): # 체크박스가 true면 이미 사용된 코드
            message = get_translation("verify_code_already_used", locale)
            await interaction.followup.send(message, ephemeral=True)
            send_verification_log(interaction.user, code, success=False, reason="이미 사용됨")
            return

        # '디스코드' 속성 확인 (이미 다른 사용자가 사용했는지 체크)
        dico_property = page_data.get("properties", {}).get("디코", {})
        stored_user_id = ""
        if dico_property.get("rich_text"):
            stored_user_id = dico_property.get("rich_text", [{}])[0].get("text", {}).get("content", "")
        
        if stored_user_id:  # 디스코드 속성이 이미 채워져 있는 경우
            if stored_user_id == str(interaction.user.id):
                # 상황 1-1: 같은 사용자 → 역할 부여하면서 체크박스 체크
                try:
                    await notion.pages.update(
                        page_id=page_id,
                        properties={
                            "✅ Buyer 역할": {
                                "checkbox": True  # 체크박스만 체크
                            }
                        }
                    )
                except Exception as e:
                    print(f"Error updating checkbox for code '{code}': {e}")
                
                # 역할 부여
                await interaction.user.add_roles(buyer_role)
                message = get_translation("verify_success", locale).format(code=code, role_name=translated_role_name)
                await interaction.followup.send(message, ephemeral=True)
                send_verification_log(interaction.user, code, success=True)
                return
            else:
                # 상황 1-2: 다른 사용자 → 거부하고 관리자에게 알림
                message = get_translation("verify_code_already_used", locale)
                await interaction.followup.send(message, ephemeral=True)
                
                # 상세한 로그로 관리자에게 알림
                admin_log_message = (
                    f"🚨 **의심스러운 인증 시도**\n"
                    f"🔑 코드: `{code}`\n"
                    f"👤 시도자/노션기록: {interaction.user.mention} (`{interaction.user.id}`) / ({stored_user_id})\n"
                    f"⚠️ 기존 노션에 기록된 id와 시도자가 다릅니다.\n"
                )
                
                if LOG_WEBHOOK_URL:
                    try:
                        requests.post(LOG_WEBHOOK_URL, json={"content": admin_log_message})
                    except Exception as e:
                        print(f"관리자 알림 전송 실패: {e}")
                
                # 기본 로그는 제거 (상세 관리자 알림으로 대체)
                return

        # 디스코드 속성이 비어있는 경우 → 정상적인 첫 인증
        # 사용자 ID 및 체크박스 업데이트 (실패해도 역할 부여는 진행)
        try:
            await notion.pages.update(
                page_id=page_id,
                properties={
                    "디코": {
                        "rich_text": [
                            {
                                "text": {
                                    "content": str(interaction.user.id) # 닉네임 대신 영구적인 사용자 ID 저장
                                }
                            }
                        ]
                    },
                    "✅ Buyer 역할": {
                        "checkbox": True  # 체크박스를 true로 설정
                    }
                }
            )
        except Exception as e:
            print(f"Error updating Notion page for code '{code}': {e}")

        # 역할 부여
        await interaction.user.add_roles(buyer_role)
        message = get_translation("verify_success", locale).format(code=code, role_name=translated_role_name)
        await interaction.followup.send(message, ephemeral=True)
        send_verification_log(interaction.user, code, success=True)

    except notion_client.errors.APIResponseError as e:
        print(f"Notion API Error during verification: {e}")
        error_message = get_translation("verify_notion_api_error", locale)
        await interaction.followup.send(error_message, ephemeral=True)
        send_verification_log(interaction.user, code, success=False, reason="API 오류")
    except Exception as e:
        print(f"An unexpected error occurred in verify command: {e}")
        error_message = get_translation("verify_discord_api_error", locale)
        await interaction.followup.send(error_message, ephemeral=True)
        send_verification_log(interaction.user, code, success=False, reason="디스코드 오류")

# 쿨다운 에러 핸들러
@verify.error
async def verify_error(interaction: discord.Interaction, error: commands.CommandError):
    if isinstance(error, commands.CommandOnCooldown):
        locale = str(interaction.locale)
        error_message = get_translation("verify_cooldown", locale).format(seconds=int(error.retry_after))
        await interaction.response.send_message(error_message, ephemeral=True)
    else:
        raise error

# 커맨드 에러 핸들러
@bot.event
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("이 명령어는 서버 관리자만 사용할 수 있습니다.", ephemeral=True)
    else:
        # 다른 종류의 에러는 콘솔에 출력
        print(f"Unhandled command tree error: {error}")
        # 사용자에게 일반적인 오류 메시지를 보낼 수도 있습니다.
        try:
            locale = str(interaction.locale)
            # 'interaction'이 만료될 수 있으므로 response/followup 시도
            if interaction.response.is_done():
                await interaction.followup.send(get_translation("verify_discord_api_error", locale), ephemeral=True)
            else:
                await interaction.response.send_message(get_translation("verify_discord_api_error", locale), ephemeral=True)
        except Exception as e:
            print(f"Failed to send generic error message to user: {e}")

@bot.event
async def on_message(message):
    # 봇 자신의 메시지는 무시
    if message.author.bot:
        return

    # 특정 채널에서만 동작 (구매자 인증 채널)
    if str(message.channel.id) == "1382415188912902258":
        # 고정(핀)된 메시지는 삭제하지 않음
        if not message.pinned:
            # 슬래시 명령어(=봇 명령)로 시작하지 않는 일반 메시지라면 삭제
            if not message.content.startswith("/"):
                await message.delete()
                return

    # 기존 on_message가 있으면 아래 줄 추가
    await bot.process_commands(message)

# 봇 실행
if not DISCORD_BOT_TOKEN:
    print("오류: DISCORD_BOT_TOKEN이 설정되지 않았습니다.")
    print("'.env' 파일에 DISCORD_BOT_TOKEN='당신의_봇_토큰' 형식으로 토큰을 추가해주세요.")
else:
    try:
        bot.run(DISCORD_BOT_TOKEN)
    except discord.errors.LoginFailure:
        print("오류: 디스코드 봇 토큰이 잘못되었습니다. 토큰을 다시 확인해주세요.")
    except Exception as e:
        print(f"봇 실행 중 오류 발생: {e}") 