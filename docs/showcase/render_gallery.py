"""README gallery: actual upstream/adapted widgets and isolated demo data."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import importlib.util
import json
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / 'media'
OUT.mkdir(parents=True, exist_ok=True)
spec = importlib.util.spec_from_file_location('sample_design', ROOT / 'docs/style-samples/render_samples.py')
design = importlib.util.module_from_spec(spec)
spec.loader.exec_module(design)
original, app = design.original, design.app
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QUrl, Qt
from PySide6.QtGui import QColor, QCursor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QListWidgetItem
from v051_behavior import V051Behavior, configure_preferences
from asset_source import configure_asset_source
from bailong_toolbar import create_toolbar
from activity_cards import create_activity_cards
from dock_controls import create_dock_controls
from companion_layout import observe_speech_layout
from chat_ui_style import make_chat_widgets
from conversation_sync import ConversationReader, make_controller_class, tool_token
from test_conversation_sync import DatabaseFixture, cached_chat
import chat_ui

now = [1000.0]
records = []


def frame(title, subtitle, footer, paint, width=1200, height=860):
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor('#fbf9ff'))
    p = QPainter(image)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    design.text(p, 50, 63, title, 34, '#5f447e', True)
    design.text(p, 50, 103, subtitle, 19, '#8a789c')
    design.card(p, width-190, 35, 140, 38, '#eee5f8', radius=12)
    design.text(p, width-169, 61, 'v0.5.1.1', 19, '#84639f', True)
    design.card(p, 42, 135, width-84, height-218, '#25232d', '#d5c5e4', 22)
    paint(p)
    design.text(p, 50, height-40, footer, 18, '#8a789c')
    p.end()
    return image


def widget_at(p, widget, rect):
    pixmap = widget.grab()
    size = pixmap.size()
    factor = min(rect.width()/size.width(), rect.height()/size.height())
    w, h = size.width()*factor, size.height()*factor
    target = QRectF(rect.x()+(rect.width()-w)/2, rect.y()+(rect.height()-h)/2, w, h)
    p.save()
    p.translate(target.topLeft())
    p.scale(factor,factor)
    widget.render(p,QPoint(0,0))
    p.restore()


def save(name, image):
    assert image.save(str(OUT / name))
    records.append({'file': name, 'kind': 'static', 'actualWidgets': True})
    print('saved '+name, flush=True)


def finish_all(pet):
    for motion in list(getattr(pet, '_ui_motions', ())):
        motion.finish()
    for window in (pet.toolbar, pet.activity_cards):
        if hasattr(window, '_reveal_animation'):
            window._reveal_animation.setCurrentTime(window._reveal_animation.duration())


def reset(pet, x=422):
    now[0] = 1000.0
    pet._ui_motion_disabled = True
    pet.set_fixed_pet(False)
    pet.dragging = False
    pet.undock()
    pet.dock_exit = None
    pet.close_notification()
    if pet.settings_panel:
        pet.settings_panel.hide()
    if pet.quota_panel:
        pet.quota_panel.hide()
    pet.bubble.hide()
    pet.bubble_timer.stop()
    pet.finish_movement()
    pet.move(x, 260)
    pet.play(pet.idles[0], 'idle')
    pet.activity_cards.set_sessions([{'key': 'demo-docs', 'title': '整理项目说明', 'project': '白龙示例项目', 'state': 'running'}])
    pet.toolbar.set_active(True)
    pet.activity_cards.set_active(True)
    pet.show()
    pet.toolbar.sync_geometry()
    app.processEvents()
    finish_all(pet)


def gif(name, pet, duration, update, fps=15, index=True):
    if os.environ.get('BAILONG_REUSE_MEDIA') == '1' and (OUT/name).is_file() and (OUT/name).stat().st_size > 10000:
        records.append({'file':name,'kind':'gif','fps':fps,'seconds':duration,'actualWidgets':True,'reused':True})
        print('reused '+name,flush=True)
        return
    pet._ui_motion_disabled = False
    tracks = {}
    kinds = set()
    for motion in list(pet._ui_motions):
        motion.clock = lambda: now[0]
    with tempfile.TemporaryDirectory(prefix='BailongGalleryFrames-') as directory:
        directory = Path(directory)
        for i in range(round(fps*duration)):
            t = i/fps
            now[0] = 1000+t
            animations = [m.animation for m in list(pet._ui_motions) if m.animation is not None]
            animations += [getattr(w, '_reveal_animation', None) for w in (pet.toolbar, pet.activity_cards)]
            for animation in [a for a in animations if a is not None]:
                entry = tracks.setdefault(id(animation), (animation, t))
                animation.setCurrentTime(min(animation.duration(), round((t-entry[1])*1000)))
            image = update(t)
            for m in list(pet._ui_motions):
                if m.animation is not None:
                    tracks.setdefault(id(m.animation), (m.animation, t))
                    kinds.add(m.kind)
            if index:
                pet.index = int(t*24) % len(pet.frames)
            assert image.save(str(directory/f'{i:03}.png'))
            if i == round(fps*duration/2):
                image.save(str(OUT/(Path(name).stem+'-poster.png')))
        command = [str(ROOT/'tools/ffmpeg.exe'), '-hide_banner', '-loglevel', 'error', '-y',
                   '-framerate', str(fps), '-i', str(directory/'%03d.png'),
                   '-filter_complex', '[0:v]split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a',
                   '-loop', '0', str(OUT/name)]
        subprocess.run(command, check=True, timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
    assert (OUT/name).stat().st_size < 10*1024**2
    records.append({'file': name, 'kind': 'gif', 'fps': fps, 'seconds': duration, 'actualWidgets': True, 'motionKinds': sorted(kinds)})
    finish_all(pet)
    print('saved '+name, flush=True)


with tempfile.TemporaryDirectory(prefix='BailongPublicGallery-') as temporary:
    temporary = Path(temporary)
    configure_preferences(original, temporary/'preferences.ini')
    configure_asset_source(original)
    class Pet(V051Behavior, original.Pet):
        toolbar = None
        exit_reason = None
        _ui_motion_disabled = True
    pet = Pet()
    pet.tick.stop(); pet.visibility_timer.stop(); pet.desktop_sync.stop()
    pet.sound_disabled = True
    pet.toolbar = create_toolbar(original, pet)
    pet.activity_cards = create_activity_cards(original, pet)
    pet.dock_controls = create_dock_controls(pet)
    pet.dock_controls.timer.stop()
    observe_speech_layout(pet)
    screen = SimpleNamespace(availableGeometry=lambda: design.AREA, name=lambda: 'readme-gallery', devicePixelRatio=lambda: 1)
    with patch.object(pet, 'screen', return_value=screen), \
         patch.object(QApplication, 'screenAt', side_effect=lambda point: screen if design.AREA.contains(point) else None), \
         patch.object(QApplication, 'screens', return_value=[screen]), \
         patch.object(original.time, 'monotonic', side_effect=lambda: now[0]):
        reset(pet)
        pet.activity_cards.set_sessions([{'key': str(i), 'title': title, 'project': project, 'state': 'running'} for i,title,project in (
            (1,'整理项目说明','白龙示例项目'), (2,'生成界面预览','界面预览'), (3,'检查任务进展','本地检查'))])
        app.processEvents(); finish_all(pet)
        pet.index = 12
        save('overview.png', design.compose(pet, '白龙、控制栏与任务卡', '完整动画 · 同会话聊天 · 跨项目任务状态',
                                            '查看任务状态，随时回到对应的 Codex 聊天', callouts=True, tag='界面演示'))

        reset(pet)
        pet.task_progress('任务进展','正在整理项目说明，已完成结构检查。',lambda:None)
        app.processEvents();finish_all(pet)
        save('task-notice.png',design.compose(pet,'任务状态，及时带到身边','任务卡展示状态，消息提示传递进展与完成结果',
                                            '示例任务与公开进展 · 点击卡片可返回对应聊天',tag='界面演示'))

        reset(pet)
        pet.show_settings(); app.processEvents(); finish_all(pet)
        save('settings.png', design.compose(pet, '常用设置，统一管理', '大小 · 自移动 · 固定桌宠 · 提醒 · 音效',
                                           '固定桌宠只限制自发位移，用户主动移动仍可执行', tag='界面演示'))
        def swap(t):
            x, dragging, caption = design.pet_position(t)
            pet.dragging = dragging
            pet.move(x, 260); pet.toolbar.sync_geometry()
            pet.index = int(t*24) % len(pet.frames)
            return design.compose(pet, '窗口换边，自然跟随', '跟随白龙移动，空间不足时切换到另一侧', caption, pointer=dragging, tag='界面演示')
        gif('window-motion.gif', pet, 8, swap, fps=20)

        reset(pet)
        flags = set()
        def dock(t):
            dragging = 1 <= t < 2.5 or 7.3 <= t < 9
            if 1 <= t < 2.5:
                pet.dragging = True
                pet.move(round(422*(1-design.smooth((t-1)/1.5))),260)
            if t >= 2.5 and 'dock' not in flags:
                pet.dragging = False; pet.move(0,260); pet.try_dock(); flags.add('dock')
            if pet.dock_edge:
                point = pet.mapToGlobal(pet.dock_rect().toAlignedRect().center()) if 4.4 <= t < 6.7 else QPoint(1050,610)
                with patch.object(QCursor, 'pos', return_value=point):
                    pet.dock_controls.refresh()
            if t >= 6.7 and 'undock' not in flags:
                pet.begin_undock(); flags.add('undock')
            if t >= 7.3:
                pet.undock(); pet.dragging = True
                pet.move(round(422*design.smooth((t-7.3)/1.7)),260)
            pet.animate()
            pet.toolbar.sync_geometry()
            caption = '拖动白龙到屏幕边缘' if t < 2.5 else ('吸边后收起控件' if t < 4.4 else ('移到探头范围，控制栏与任务卡浮现' if t < 6.7 else '展开白龙，继续拖动'))
            return design.compose(pet, '左右吸边，收放自如', '保留原作者吸边动作，控制栏与任务卡随悬停显隐', caption, pointer=dragging, tag='界面演示')
        gif('dock-hover.gif', pet, 9.8, dock, index=False)

        reset(pet,350)
        pet.set_fixed_pet(True); pet.show_settings(); app.processEvents(); finish_all(pet)
        fixed_flags = set()
        def fixed(t):
            if t >= 1 and 'close' not in fixed_flags:
                pet.show_settings(); fixed_flags.add('close')
            if t >= 1.8 and 'auto' not in fixed_flags:
                pet.movement_ready_at=0; pet.start_movement(pet.movement_slots[0]); fixed_flags.add('auto')
            if 1.8 <= t < 4.2:
                item=pet.current; start=item.get('moveStartMs',0)/1000; stop=item.get('moveStopMs',item['durationMs'])/1000
                elapsed=start+(stop-start)*min(1,(t-1.8)/2.2)
                pet.advance_movement(elapsed); pet.index=min(len(pet.frames)-1,int(elapsed*24))
                assert pet.x()==350
            if t >= 4.2 and 'manual' not in fixed_flags:
                pet.finish_movement(); pet.start_movement(pet.movement_slots[0],manual=True); fixed_flags.add('manual')
            if 4.2 <= t < 6.6:
                item=pet.current; start=item.get('moveStartMs',0)/1000; stop=item.get('moveStopMs',item['durationMs'])/1000
                elapsed=start+(stop-start)*min(1,(t-4.2)/2.2)
                pet.advance_movement(elapsed); pet.index=min(len(pet.frames)-1,int(elapsed*24))
            if 6.6 <= t < 7.8:
                if 'restore' not in fixed_flags:
                    fixed_flags.add('restore'); fixed_flags.add(('origin',pet.x())); pet.finish_movement(); pet.play(pet.idles[0],'idle')
                origin=next(v[1] for v in fixed_flags if isinstance(v,tuple))
                pet.dragging=True;pet.move(round(origin+(350-origin)*design.smooth((t-6.6)/1.2)),260)
            if t >= 7.8 and 'settings' not in fixed_flags:
                pet.dragging=False;pet.move(350,260);pet.show_settings();fixed_flags.add('settings')
            pet.toolbar.sync_geometry()
            caption = '开启“固定桌宠”' if t<1.8 else ('自发移动：原地播放，位置保持不变' if t<4.2 else ('手动移动：仍然接受用户指令' if t<6.6 else '用户拖动仍有效，设置会自动保存'))
            return design.compose(pet, '固定位置，保留动作', '自发位移可固定，用户主动操作仍然有效', caption, pointer=pet.dragging, tag='界面演示')
        gif('fixed-pet.gif',pet,9,fixed,index=False)

        reset(pet)
        original_composer=chat_ui.Composer()
        Composer,Window=make_chat_widgets(chat_ui.Composer,chat_ui.ChatWindow)
        styled_composer=Composer()
        for widget in (original_composer,styled_composer):
            widget.editor.setPlainText('帮我整理项目说明，并继续这段对话。')
            widget.show()
        app.processEvents()
        def compare(p):
            design.text(p,90,210,'原作者 v0.5.1',25,'#e4d7ef',True)
            design.text(p,650,210,'本适配版 v0.5.1.1',25,'#e4d7ef',True)
            widget_at(p,original_composer,QRectF(75,260,500,340))
            widget_at(p,styled_composer,QRectF(635,260,500,340))
            design.text(p,90,685,'保留原作者的聊天、历史与发送能力',18,'#b5a6c3')
            design.text(p,650,685,'统一圆角、留白、按钮与字号',18,'#b5a6c3')
        save('interface-comparison.png',frame('保留原作能力，统一界面体验','相同输入内容 · 原作者聊天小窗与适配版聊天小窗',
                                             '界面直接渲染自原作者 v0.5.1 与本适配代码，使用同一条示例输入',compare))
        original_composer.hide();styled_composer.hide()

        fixture=DatabaseFixture(temporary/'codex'); fixture.thread();fixture.turn()
        fixture.write(fixture.state,'UPDATE threads SET name=?,title=?,cwd=?',('整理项目说明','整理项目说明','D:/白龙示例项目'))
        fixture.write(fixture.history,'UPDATE thread_turns SET started_at=?,completed_at=?',(1793260800,1793260810))
        fixture.user('user-1','请将窗口间距改为 12，并保留完整设置面板。',1)
        fixture.item('reason-1',{'type':'reasoning','summary':[{'text':'检查设置布局，并保留用户已有配置。'}]},2)
        fixture.item('cmd-1',{'type':'commandExecution','command':'python -m unittest','cwd':'D:/白龙示例项目',
                     'status':'completed','exitCode':0,'durationMs':1250,'aggregatedOutput':'布局与设置检查通过。'},3)
        fixture.change(diff='@@ -1,4 +1,4 @@\n-gap = 8\n+gap = 12\n width = 350\n-height = 430\n+height = 460\n fixed_size = True',ordinal=4)
        fixture.agent('agent-1','已完成调整。你可以展开文件编辑记录，查看原文件与新文件的对应变化。',5)
        data=temporary/'chat-data';data.mkdir()
        cached=cached_chat();cached['cwd']='D:/白龙示例项目'
        (data/'conversations.json').write_text(json.dumps([cached],ensure_ascii=False),encoding='utf-8')
        Controller=make_controller_class(chat_ui.ChatController,background=False)
        with patch.object(chat_ui,'Composer',Composer),patch.object(chat_ui,'ChatWindow',Window):
            controller=Controller(pet,directory=data)
        controller.conversation_sync.reader=ConversationReader(fixture.root)
        controller.conversation_sync.apply(controller.conversation_sync.reader.read(['thread-1']))
        controller.open_history();app.processEvents()
        window=controller.window
        window.resize(1040,600);app.processEvents()
        window.view.verticalScrollBar().setValue(0)
        def focus_tool(item):
            for _ in range(3):app.processEvents()
            href='bailong-tool:'+tool_token('thread-1',item)
            cursor=controller._history_link_cursor(href)
            if cursor is not None:
                bar=window.view.verticalScrollBar()
                bar.setValue(bar.value()+window.view.cursorRect(cursor).top()-24)
            controller.file_views.update_positions()
        def history_frame(title,subtitle,footer):
            window.path_label.setText('任务文件夹：D:/白龙示例项目')
            return frame(title,subtitle,footer,lambda p:widget_at(p,window,QRectF(62,150,1076,614)))
        save('chat-history.png',history_frame('同一段对话，持续接续','聊天、公开摘要与工具记录集中展示',
                                             '示例会话 · 摘要与工具详情默认收起 · 打开历史定位最新'))
        states=set()
        def details(t):
            phase=0 if t<1 else (1 if t<2.5 else (2 if t<4.5 else 3))
            if phase not in states:
                states.add(phase)
                if phase==1:window.view.anchorClicked.emit(QUrl('bailong-tool:'+tool_token('thread-1','cmd-1')))
                if phase==2:
                    window.view.anchorClicked.emit(QUrl('bailong-tool:'+tool_token('thread-1','cmd-1')))
                    window.view.anchorClicked.emit(QUrl('bailong-tool:'+tool_token('thread-1','file-1')))
                if phase==3:window.view.anchorClicked.emit(QUrl('bailong-tool:'+tool_token('thread-1','file-1')))
                app.processEvents()
                window.path_label.setText('任务文件夹：D:/白龙示例项目')
                if phase in (1,2):focus_tool('cmd-1' if phase==1 else 'file-1')
            return history_frame('详情按需展开，阅读更清楚','命令简化显示 · 文件变化左右对照',
                                 ('默认收起','点击查看命令与执行结果','原文件 / 新文件，逐处查看删改','收起后继续阅读')[phase])
        gif('tool-details.gif',pet,5.5,details,fps=12,index=False)
        window.view.anchorClicked.emit(QUrl('bailong-tool:'+tool_token('thread-1','file-1')));app.processEvents()
        focus_tool('file-1')
        assert controller.file_views.panels
        save('file-diff.png',history_frame('文件怎么改，左右对照看','原文件 / 新文件 · 标出删改 · 新增文件完整展示',
                                         '文件代码区独立横向滚动，普通聊天保持自动换行'))
        controller.shutdown();window.hide();controller.composer.hide()

        reset(pet)
        from quota_panel import create_quota_panel
        if pet.quota_panel is None:pet.quota_panel=create_quota_panel(pet)
        pet.quota_panel.display([{'name':'5 小时','remaining':61,'resetsAt':1793260800},
                                 {'name':'一周','remaining':94,'resetsAt':1793865600}])
        pet.say('让我看看额度还有多少……',4000)
        pet.quota_panel.show();pet.quota_panel.reposition();app.processEvents();finish_all(pet)
        save('quota.png',design.compose(pet,'查额度，也有白龙的回应','查询台词 · 圆角额度条 · 自动收起',
                                       '界面为示例额度数据；真实查询使用你自己的 Codex 账号',tag='界面演示'))
        pet.exit_reason='gallery-complete';pet.close();app.processEvents()

report={'actualWidgets':True,'upstreamVersion':'0.5.1','adaptationVersion':'0.5.1.1',
        'preferencesIsolated':True,'sampleConversationsOnly':True,'modelRequests':0,'media':records}
(OUT/'gallery-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'media':len(records),'files':[r['file'] for r in records]},ensure_ascii=False),flush=True)
