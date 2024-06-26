#!/usr/bin/env python3
from requests import utils as rutils
from aiofiles.os import path as aiopath, remove as aioremove, listdir, makedirs
from os import walk, path as ospath
from html import escape
from aioshutil import move
from asyncio import create_subprocess_exec, sleep, Event

from bot import Interval, aria2, DOWNLOAD_DIR, download_dict, download_dict_lock, LOGGER, DATABASE_URL, \
    MAX_SPLIT_SIZE, config_dict, status_reply_dict_lock, user_data, \
    GLOBAL_EXTENSION_FILTER, IS_PREMIUM_USER, OWNER_ID
from bot.helper.ext_utils.bot_utils import sync_to_async, get_readable_file_size, is_gdrive_id, get_telegraph_list
from bot.helper.ext_utils.fs_utils import get_base_name, get_path_size, clean_download, clean_target, \
    is_first_archive_split, is_archive, is_archive_split, join_files
from bot.helper.ext_utils.leech_utils import split_file
from bot.helper.ext_utils.exceptions import NotSupportedExtractionArchive
from bot.helper.mirror_utils.status_utils.extract_status import ExtractStatus
from bot.helper.mirror_utils.status_utils.zip_status import ZipStatus
from bot.helper.mirror_utils.status_utils.split_status import SplitStatus
from bot.helper.mirror_utils.status_utils.gdrive_status import GdriveStatus
from bot.helper.mirror_utils.status_utils.telegram_status import TelegramStatus
from bot.helper.mirror_utils.status_utils.rclone_status import RcloneStatus
from bot.helper.mirror_utils.gdrive_utlis.upload import gdUpload
from bot.helper.mirror_utils.upload_utils.pyrogramEngine import TgUploader
from bot.helper.mirror_utils.rclone_utils.transfer import RcloneTransferHelper
from bot.helper.telegram_helper.message_utils import sendMessage, delete_all_messages, update_all_messages
from bot.helper.telegram_helper.button_build import ButtonMaker
from bot.helper.ext_utils.db_handler import DbManger
from bot.helper.ext_utils.atrocious_utils import check_duplicate_file, get_bot_pm_button, send_to_chat


class MirrorLeechListener:
    def __init__(self, message, compress=False, extract=False, isQbit=False, isLeech=False, tag=None, select=False, seed=False, sameDir=None, rcFlags=None, upDest=None, join=False):
        if sameDir is None:
            sameDir = {}
        self.message = message
        self.uid = self.message.id
        self.extract = extract
        self.compress = compress
        self.isQbit = isQbit
        self.isLeech = isLeech
        self.tag = tag
        self.seed = seed
        self.newDir = ""
        self.dir = f"{DOWNLOAD_DIR}{self.uid}"
        self.select = select
        self.isSuperGroup = self.message.chat.type.name in ['SUPERGROUP', 'CHANNEL']
        self.suproc = None
        self.sameDir = sameDir
        self.rcFlags = rcFlags
        self.upDest = upDest
        self.join = join
        self.user_id = self.message.from_user.id
        self.user_dict = user_data.get(self.user_id, {})

    async def clean(self):
        try:
            async with status_reply_dict_lock:
                if Interval:
                    Interval[0].cancel()
                    Interval.clear()
            await sync_to_async(aria2.purge)
            await delete_all_messages()
        except:
            pass

    async def onDownloadStart(self):
        if self.isSuperGroup and config_dict['INCOMPLETE_TASK_NOTIFIER'] and DATABASE_URL:
            await DbManger().add_incomplete_task(self.message.chat.id, self.message.link, self.tag)

    async def onDownloadComplete(self):
        multi_links = False
        while True:
            if self.sameDir:
                if self.sameDir['total'] in [1, 0] or self.sameDir['total'] > 1 and len(self.sameDir['tasks']) > 1:
                    break
            else:
                break
            await sleep(0.2)

        async with download_dict_lock:
            if self.sameDir and self.sameDir['total'] > 1:
                self.sameDir['tasks'].remove(self.uid)
                self.sameDir['total'] -= 1
                folder_name = self.sameDir['name']
                spath = f"{self.dir}/{folder_name}"
                des_path = f"{DOWNLOAD_DIR}{list(self.sameDir['tasks'])[0]}/{folder_name}"
                await makedirs(des_path, exist_ok=True)
                for item in await listdir(spath):
                    if item.endswith(('.aria2', '.!qB')):
                        continue
                    item_path = f"{self.dir}/{folder_name}/{item}"
                    if item in await listdir(des_path):
                        await move(item_path, f'{des_path}/{self.uid}-{item}')
                    else:
                        await move(item_path, f'{des_path}/{item}')
                multi_links = True
            download = download_dict[self.uid]
            name = str(download.name()).replace('/', '')
            gid = download.gid()
        LOGGER.info(f"Download completed: {name}")

        if multi_links:
            await self.onUploadError('Downloaded! Waiting for other tasks...')
            return

        if name == "None" or self.isQbit or not await aiopath.exists(f"{self.dir}/{name}"):
            try:
                files = await listdir(self.dir)
            except Exception as e:
                await self.onUploadError(str(e))
                return
            name = files[-1]
            if name == "yt-dlp-thumb":
                name = files[0]

        dl_path = f"{self.dir}/{name}"
        up_path = ''
        size = await get_path_size(dl_path)
        
        if self.join and await aiopath.isdir(dl_path):
            await join_files(dl_path)

        if self.isLeech:
            LEECH_SPLIT_SIZE = self.user_dict.get('split_size') or config_dict['LEECH_SPLIT_SIZE']
            if self.upDest.startswith('b:') and LEECH_SPLIT_SIZE > 2097152000:
                LEECH_SPLIT_SIZE = 2097152000
                user_leech = False
            elif self.upDest.startswith('u:') and IS_PREMIUM_USER and LEECH_SPLIT_SIZE > 4194304000:
                LEECH_SPLIT_SIZE = 4194304000
                user_leech = True
            else:
                user_leech = (self.user_dict.get('user_leech') or config_dict['USER_LEECH'] and 'user_leech' not in self.user_dict) and IS_PREMIUM_USER

            MAX_SIZE = MAX_SPLIT_SIZE if user_leech else 2097152000
            LEECH_SPLIT_SIZE = min(LEECH_SPLIT_SIZE, MAX_SIZE)
        else:
            LEECH_SPLIT_SIZE = 0

        if self.extract:
            pswd = self.extract if isinstance(self.extract, str) else ''
            try:
                if await aiopath.isfile(dl_path):
                    up_path = get_base_name(dl_path)
                LOGGER.info(f"Extracting: {name}")
                async with download_dict_lock:
                    download_dict[self.uid] = ExtractStatus(
                        name, size, gid, self)
                if await aiopath.isdir(dl_path):
                    if self.seed:
                        self.newDir = f"{self.dir}10000"
                        up_path = f"{self.newDir}/{name}"
                    else:
                        up_path = dl_path
                    for dirpath, _, files in await sync_to_async(walk, dl_path, topdown=False):
                        for file_ in files:
                            if is_first_archive_split(file_) or is_archive(file_) and not file_.endswith('.rar'):
                                f_path = ospath.join(dirpath, file_)
                                t_path = dirpath.replace(
                                    self.dir, self.newDir) if self.seed else dirpath
                                cmd = [
                                    "7z", "x", f"-p{pswd}", f_path, f"-o{t_path}", "-aot", "-xr!@PaxHeader"]
                                if not pswd:
                                    del cmd[2]
                                if self.suproc == 'cancelled' or self.suproc is not None and self.suproc.returncode == -9:
                                    return
                                self.suproc = await create_subprocess_exec(*cmd)
                                code = await self.suproc.wait()
                                if code == -9:
                                    return
                                elif code != 0:
                                    LOGGER.error(
                                        'Unable to extract archive splits!')
                        if not self.seed and self.suproc is not None and self.suproc.returncode == 0:
                            for file_ in files:
                                if is_archive_split(file_) or is_archive(file_):
                                    del_path = ospath.join(dirpath, file_)
                                    try:
                                        await aioremove(del_path)
                                    except:
                                        return
                else:
                    if self.seed:
                        self.newDir = f"{self.dir}10000"
                        up_path = up_path.replace(self.dir, self.newDir)
                    cmd = ["7z", "x", f"-p{pswd}", dl_path,
                           f"-o{up_path}", "-aot", "-xr!@PaxHeader"]
                    if not pswd:
                        del cmd[2]
                    if self.suproc == 'cancelled':
                        return
                    self.suproc = await create_subprocess_exec(*cmd)
                    code = await self.suproc.wait()
                    if code == -9:
                        return
                    elif code == 0:
                        LOGGER.info(f"Extracted Path: {up_path}")
                        if not self.seed:
                            try:
                                await aioremove(dl_path)
                            except:
                                return
                    else:
                        LOGGER.error(
                            'Unable to extract archive! Uploading anyway')
                        self.newDir = ""
                        up_path = dl_path
            except NotSupportedExtractionArchive:
                LOGGER.info("Not any valid archive, uploading file as it is.")
                self.newDir = ""
                up_path = dl_path

        if self.compress:
            pswd = self.compress if isinstance(self.compress, str) else ''
            if up_path:
                dl_path = up_path
                up_path = f"{up_path}.zip"
            elif self.seed and self.isLeech:
                self.newDir = f"{self.dir}10000"
                up_path = f"{self.newDir}/{name}.zip"
            else:
                up_path = f"{dl_path}.zip"
            async with download_dict_lock:
                download_dict[self.uid] = ZipStatus(name, size, gid, self)
            cmd = ["7z", f"-v{LEECH_SPLIT_SIZE}b", "a",
                   "-mx=0", f"-p{pswd}", up_path, dl_path]
            for ext in GLOBAL_EXTENSION_FILTER:
                ex_ext = f'-xr!*.{ext}'
                cmd.append(ex_ext)
            if self.isLeech and int(size) > LEECH_SPLIT_SIZE:
                if not pswd:
                    del cmd[4]
                LOGGER.info(
                    f'Zip: orig_path: {dl_path}, zip_path: {up_path}.0*')
            else:
                del cmd[1]
                if not pswd:
                    del cmd[3]
                LOGGER.info(f'Zip: orig_path: {dl_path}, zip_path: {up_path}')
            if self.suproc == 'cancelled':
                return
            self.suproc = await create_subprocess_exec(*cmd)
            code = await self.suproc.wait()
            if code == -9:
                return
            elif not self.seed:
                await clean_target(dl_path)

        if not self.compress and not self.extract:
            up_path = dl_path

        up_dir, up_name = up_path.rsplit('/', 1)
        size = await get_path_size(up_dir)
        if self.isLeech:
            m_size = []
            o_files = []
            if not self.compress:
                checked = False
                for dirpath, _, files in await sync_to_async(walk, up_dir, topdown=False):
                    for file_ in files:
                        f_path = ospath.join(dirpath, file_)
                        f_size = await aiopath.getsize(f_path)
                        if f_size > LEECH_SPLIT_SIZE:
                            if not checked:
                                checked = True
                                async with download_dict_lock:
                                    download_dict[self.uid] = SplitStatus(
                                        up_name, size, gid, self)
                                LOGGER.info(f"Splitting: {up_name}")
                            res = await split_file(f_path, f_size, file_, dirpath, LEECH_SPLIT_SIZE, LEECH_SPLIT_SIZE, MAX_SIZE, self)
                            if not res:
                                return
                            if res == "errored":
                                if f_size <= MAX_SIZE:
                                    continue
                                try:
                                    await aioremove(f_path)
                                except:
                                    return
                            elif not self.seed or self.newDir:
                                try:
                                    await aioremove(f_path)
                                except:
                                    return
                            else:
                                m_size.append(f_size)
                                o_files.append(file_)

        if self.isLeech:
            size = await get_path_size(up_dir)
            for s in m_size:
                size = size - s
            LOGGER.info(f"Leech Name: {up_name}")
            tg = TgUploader(up_name, up_dir, self)
            tg_upload_status = TelegramStatus(
                tg, size, self.message, gid, 'up')
            async with download_dict_lock:
                download_dict[self.uid] = tg_upload_status
            await update_all_messages()
            await tg.upload(o_files, m_size, size)
        elif is_gdrive_id(self.upDest):
            msg, button = await check_duplicate_file(self, up_name)
            if msg:
                await self.onUploadError(msg, button)
                return
            LOGGER.info(f"Upload Name: {up_name}")
            size = await get_path_size(up_path)
            drive = gdUpload(up_name, up_dir, self)
            upload_status = GdriveStatus(drive, size, self.message, gid, 'up')
            async with download_dict_lock:
                download_dict[self.uid] = upload_status
            await update_all_messages()
            await sync_to_async(drive.upload, size)
        else:
            size = await get_path_size(up_path)
            LOGGER.info(f"Upload Name: {up_name}")
            RCTransfer = RcloneTransferHelper(self, up_name)
            async with download_dict_lock:
                download_dict[self.uid] = RcloneStatus(
                    RCTransfer, self.message, gid, 'up')
            await update_all_messages()
            await RCTransfer.upload(up_path, size)

    async def onUploadComplete(self, link, size, files, folders, mime_type, name, rclonePath='', dir_id='', private=False):
        if self.isSuperGroup and config_dict['INCOMPLETE_TASK_NOTIFIER'] and DATABASE_URL:
            await DbManger().rm_complete_task(self.message.link)
        if config_dict['SAFE_MODE']:
            gpmsg = f"<b>Name: </b>Safe Mode Enabled"
        else:
            gpmsg = f"<b>Name: </b><code>{escape(name)}</code>"
        bpmsg = f"<b>Name: </b><code>{escape(name)}</code>"
        msg = f'\n\n<b>• Size: </b>{get_readable_file_size(size)}'
        LOGGER.info(f'Task Done: {name}')
        if self.isLeech:
            msg += f'\n<b>• Total Files: </b>{folders}'
            if mime_type != 0:
                msg += f'\n<b>Corrupted Files: </b>{mime_type}'
            msg += f'\n\n<b>• User: </b>{self.tag}'
            msg += f'\n<b>• User ID: </b> <code>{self.message.from_user.id}</code>\n\n'
            if not files:
                await sendMessage(self.message, gpmsg + msg)
            else:
                fmsg = ''
                for index, (link, name) in enumerate(files.items(), start=1):
                    fmsg += f"{index}. <a href='{link}'>{name}</a>\n\n"
                    if len(fmsg.encode() + msg.encode()) > 4000:
                        await sendMessage(self.message, msg + fmsg)
                        await sleep(1)
                        fmsg = ''
                if fmsg != '':
                    if config_dict['BOT_PM'] and self.message.chat.type != self.message.chat.type.PRIVATE:
                        lbpmsg = f'<b>Files has been sent in private.</b>'
                        button = await get_bot_pm_button()
                        if config_dict['SAFE_MODE']:
                            if self.message.from_user.id == OWNER_ID and self.message.chat.type == self.message.chat.type.PRIVATE:
                                await sendMessage(self.message, gpmsg + msg + fmsg + lbpmsg, button)
                            else:
                                await sendMessage(self.message, gpmsg + msg + lbpmsg, button)
                        else:
                            await sendMessage(self.message, gpmsg + msg + fmsg + lbpmsg, button)
                    else:
                        await sendMessage(self.message, gpmsg + msg + fmsg)
            if self.seed:
                if self.newDir:
                    await clean_target(self.newDir)
                return
        else:
            msg += f'\n<b>• Type: </b>{mime_type}'
            if mime_type == "Folder":
                msg += f'\n<b>• SubFolders: </b>{folders}'
                msg += f'\n<b>• Files: </b>{files}'
            if link or rclonePath and config_dict['RCLONE_SERVE_URL'] and not private:
                buttons = ButtonMaker()
                if link:
                    if config_dict['DISABLE_DRIVE_LINK']:
                        if self.message.from_user.id == OWNER_ID and self.message.chat.type == self.message.chat.type.PRIVATE: 
                            buttons.ubutton("☁️ Drive Link", link)
                        else:
                            pass
                    else:
                        buttons.ubutton("☁️ Drive Link", link)
                else:
                    msg += f'\n\n• Path: <code>{rclonePath}</code>'
                if rclonePath and (RCLONE_SERVE_URL := config_dict['RCLONE_SERVE_URL']) and not private:
                    remote, path = rclonePath.split(':', 1)
                    url_path = rutils.quote(f'{path}')
                    share_url = f'{RCLONE_SERVE_URL}/{remote}/{url_path}'
                    if mime_type == "Folder":
                        share_url += '/'
                    buttons.ubutton("🔗 Rclone Link", share_url)
                if not rclonePath and dir_id:
                    INDEX_URL = ''
                    if private:
                        INDEX_URL = self.user_dict['index_url'] if self.user_dict.get('index_url') else ''
                    elif config_dict['INDEX_URL']:
                        INDEX_URL = config_dict['INDEX_URL']
                    if INDEX_URL:
                        share_url = f'{INDEX_URL}findpath?id={dir_id}'
                        buttons.ubutton("⚡ Index Link", share_url)
                        if mime_type.startswith(('image', 'video', 'audio')):
                            share_urls = f'{INDEX_URL}findpath?id={dir_id}&view=true'
                            buttons.ubutton("🌐 View Link", share_urls)
                button = buttons.build_menu(2)
            else:
                msg += f'\n\n• Path: <code>{rclonePath}</code>'
                button = None
            msg += f'\n\n<b>• User: </b>{self.tag}'
            msg += f'\n<b>• User ID: </b> <code>{self.message.from_user.id}</code>'
            if config_dict['BOT_PM'] and self.message.chat.type != self.message.chat.type.PRIVATE:
                bmsg = f'\n\n<b>Links has been sent in private.</b>'
                await send_to_chat(chat_id=self.message.from_user.id, text=bpmsg + msg, button=button, photo=True)
                pmbutton = await get_bot_pm_button()
                await sendMessage(self.message, gpmsg+msg+bmsg, pmbutton)
            else:
                await sendMessage(self.message, gpmsg+msg, button)
            if config_dict['MIRROR_LOG_CHAT'] and self.message.from_user.id != OWNER_ID:
                await send_to_chat(chat_id=config_dict['MIRROR_LOG_CHAT'], text=bpmsg+msg, button=button)
            if self.seed:
                if self.newDir:
                    await clean_target(self.newDir)
                elif self.compress:
                    await clean_target(f"{self.dir}/{name}")
                return

        await clean_download(self.dir)
        async with download_dict_lock:
            if self.uid in download_dict.keys():
                del download_dict[self.uid]
            count = len(download_dict)
        if count == 0:
            await self.clean()
        else:
            await update_all_messages()

    async def onDownloadError(self, error, button=None):
        async with download_dict_lock:
            if self.uid in download_dict.keys():
                del download_dict[self.uid]
            count = len(download_dict)
            if self.sameDir and self.uid in self.sameDir['tasks']:
                self.sameDir['tasks'].remove(self.uid)
                self.sameDir['total'] -= 1
        msg = f"Hey {self.tag}.\n\n{escape(error)}"
        await sendMessage(self.message, msg, button)
        if count == 0:
            await self.clean()
        else:
            await update_all_messages()

        if self.isSuperGroup and config_dict['INCOMPLETE_TASK_NOTIFIER'] and DATABASE_URL:
            await DbManger().rm_complete_task(self.message.link)

        await sleep(3)
        await clean_download(self.dir)
        if self.newDir:
            await clean_download(self.newDir)

    async def onUploadError(self, error, button=None):
        async with download_dict_lock:
            if self.uid in download_dict.keys():
                del download_dict[self.uid]
            count = len(download_dict)
        await sendMessage(self.message, f"Hey {self.tag}.\n{escape(error)}", button)
        if count == 0:
            await self.clean()
        else:
            await update_all_messages()

        if self.isSuperGroup and config_dict['INCOMPLETE_TASK_NOTIFIER'] and DATABASE_URL:
            await DbManger().rm_complete_task(self.message.link)

        await sleep(3)
        await clean_download(self.dir)
        if self.newDir:
            await clean_download(self.newDir)
