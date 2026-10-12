#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Build script for 漫剧生成系统 frontend - 整合版 v3"""
import os

CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#f8f9fa;--card:#fff;--border:#e5e7eb;--text:#1f2937;--t2:#6b7280;--accent:#2563eb;--accent2:#7c3aed;--success:#10b981;--warn:#f59e0b;--danger:#ef4444}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);height:100vh;overflow:hidden}
.app{display:flex;height:100vh}
.nav{width:60px;background:#1e293b;display:flex;flex-direction:column;align-items:center;padding:12px 0;gap:4px;flex-shrink:0}
.nav-item{width:44px;height:44px;border-radius:10px;display:grid;place-items:center;color:#94a3b8;cursor:pointer;transition:.2s}
.nav-item:hover{background:rgba(255,255,255,.1);color:#fff}
.nav-item.on{background:var(--accent);color:#fff}
.nav-logo{width:40px;height:40px;background:linear-gradient(135deg,var(--accent),var(--accent2));border-radius:12px;display:grid;place-items:center;color:#fff;font-weight:700;font-size:1.1rem;margin-bottom:12px}
.nav-spacer{flex:1}
.main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.topbar{height:56px;background:var(--card);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 20px;gap:16px;flex-shrink:0}
.topbar-title{font-weight:600;font-size:1.05rem}
.topbar-sub{color:var(--t2);font-size:.82rem}
.topbar-actions{margin-left:auto;display:flex;gap:8px}
.content{flex:1;overflow-y:auto;padding:20px}
.phase-nav{display:flex;gap:4px;padding:16px 20px;background:var(--card);border-bottom:1px solid var(--border);overflow-x:auto}
.phase-item{padding:8px 16px;border-radius:8px;font-size:.82rem;cursor:pointer;white-space:nowrap;transition:.2s;color:var(--t2)}
.phase-item:hover{background:#f3f4f6}
.phase-item.on{background:var(--accent);color:#fff}
.card{background:var(--card);border-radius:12px;border:1px solid var(--border);padding:16px;margin-bottom:12px}
.card-title{font-weight:600;font-size:.9rem;margin-bottom:10px;display:flex;align-items:center;gap:8px}
.btn{padding:8px 16px;border-radius:8px;border:none;font-size:.82rem;cursor:pointer;display:inline-flex;align-items:center;gap:6px;transition:.2s;font-weight:500}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{background:#1d4ed8}
.btn-dark{background:#1e293b;color:#fff}
.btn-dark:hover{background:#334155}
.btn-ghost{background:transparent;color:var(--t2);border:1px solid var(--border)}
.btn-ghost:hover{background:#f3f4f6}
.btn-danger{background:var(--danger);color:#fff}
.btn-sm{padding:6px 12px;font-size:.78rem}
.btn:disabled{opacity:.5;cursor:not-allowed}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.card-proj{padding:0;cursor:pointer;transition:.2s}
.card-proj:hover{border-color:var(--accent);transform:translateY(-2px)}
.card-proj-body{padding:14px}
.card-proj-name{font-weight:600;font-size:.95rem;margin-bottom:4px}
.card-proj-meta{color:var(--t2);font-size:.75rem;display:flex;gap:12px}
.form-group{margin-bottom:14px}
.form-label{display:block;font-size:.82rem;font-weight:500;margin-bottom:6px;color:var(--t2)}
.form-input{width:100%;padding:10px 12px;border:1px solid var(--border);border-radius:8px;font-size:.88rem;outline:none;transition:.2s}
.form-input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(37,99,235,.1)}
.chat-panel{width:360px;background:var(--card);border-left:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0}
.chat-head{padding:14px 16px;border-bottom:1px solid var(--border);font-weight:600;font-size:.9rem;display:flex;align-items:center;gap:8px}
.chat-head i{color:var(--accent)}
.chat-msgs{flex:1;overflow-y:auto;padding:16px}
.chat-msg{margin-bottom:12px;padding:10px 14px;border-radius:10px;font-size:.85rem;line-height:1.5}
.chat-msg.bot{background:#f0f7ff;border:1px solid #dbeafe}
.chat-msg.user{background:var(--accent);color:#fff;margin-left:40px}
.chat-input{padding:12px 16px;border-top:1px solid var(--border);display:flex;gap:8px}
.chat-input input{flex:1;padding:10px 12px;border:1px solid var(--border);border-radius:8px;font-size:.85rem;outline:none}
.chat-input input:focus{border-color:var(--accent)}
.chat-send{width:40px;height:40px;border-radius:8px;border:none;background:var(--accent);color:#fff;cursor:pointer;display:grid;place-items:center}
.chat-send:hover{background:#1d4ed8}
.status{display:inline-flex;align-items:center;gap:4px;padding:3px 8px;border-radius:4px;font-size:.72rem;font-weight:500}
.status-running{background:#dbeafe;color:#1e40af}
.status-done{background:#d1fae5;color:#065f46}
.status-fail{background:#fee2e2;color:#991b1b}
.status-wait{background:#f3f4f6;color:#6b7280}
.progress{height:6px;background:#e5e7eb;border-radius:3px;overflow:hidden;margin:8px 0}
.progress-bar{height:100%;background:var(--accent);border-radius:3px;transition:width .3s}
.modal-mask{position:fixed;inset:0;background:rgba(0,0,0,.4);display:none;place-items:center;z-index:100}
.modal-mask.on{display:grid}
.modal{background:var(--card);border-radius:16px;width:90%;max-width:520px;max-height:85vh;overflow:hidden;display:flex;flex-direction:column}
.modal-head{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.modal-title{font-weight:600;font-size:1rem}
.modal-close{width:32px;height:32px;border-radius:8px;border:none;background:transparent;cursor:pointer;font-size:1.1rem;display:grid;place-items:center;color:var(--t2)}
.modal-close:hover{background:#f3f4f6}
.modal-body{padding:20px;overflow-y:auto;flex:1}
.toast-box{position:fixed;top:20px;right:20px;z-index:200}
.toast{padding:10px 16px;border-radius:8px;margin-bottom:8px;font-size:.82rem;display:flex;align-items:center;gap:6px;animation:fadeIn .3s}
.toast.ok{background:#d1fae5;color:#065f46}
.toast.er{background:#fee2e2;color:#991b1b}
.toast.wn{background:#fef3c7;color:#92400e}
@keyframes fadeIn{from{opacity:0;transform:translateY(-10px)}to{opacity:1;transform:translateY(0)}}
.empty{text-align:center;padding:40px 20px;color:var(--t2)}
.empty i{font-size:3rem;margin-bottom:12px;display:block;color:#d1d5db}
.auto-panel{background:linear-gradient(135deg,#1e293b,#334155);color:#fff;border-radius:12px;padding:20px;margin-bottom:16px}
.auto-panel h3{font-size:1rem;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.auto-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.auto-stat{text-align:center}
.auto-stat-val{font-size:1.5rem;font-weight:700}
.auto-stat-label{font-size:.72rem;color:#94a3b8;margin-top:2px}
.prod-list{display:flex;flex-direction:column;gap:8px}
.prod-item{display:flex;align-items:center;gap:12px;padding:10px 14px;background:#f9fafb;border-radius:8px}
.prod-item-name{font-weight:500;font-size:.85rem;min-width:100px}
.prod-item-progress{flex:1}
.prod-item-status{min-width:80px;text-align:right}
"""

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>漫剧生成系统</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
<style>""" + CSS + """</style>
</head>
<body>
<div class="app">
  <div class="nav">
    <div class="nav-logo">漫</div>
    <div class="nav-item on" id="navHome" onclick="goHome()" title="项目列表"><i class="bi bi-grid"></i></div>
    <div class="nav-item" id="navNew" onclick="openNew()" title="新建项目"><i class="bi bi-plus-lg"></i></div>
    <div class="nav-item" id="navAuto" onclick="goAuto()" title="自动生产"><i class="bi bi-robot"></i></div>
    <div class="nav-spacer"></div>
    <div class="nav-item" onclick="openSet()" title="AI 设置"><i class="bi bi-gear"></i></div>
  </div>
  <div class="main">
    <div class="topbar">
      <div>
        <div class="topbar-title" id="pageTitle">我的项目</div>
        <div class="topbar-sub" id="pageSub">管理您的所有短剧项目</div>
      </div>
      <div class="topbar-actions" id="topActions"></div>
    </div>
    <div class="phase-nav" id="phaseNav" style="display:none">
      <div class="phase-item on" data-phase="1" onclick="switchPhase(1)">1. 剧本生成</div>
      <div class="phase-item" data-phase="2" onclick="switchPhase(2)">2. 资产生成</div>
      <div class="phase-item" data-phase="3" onclick="switchPhase(3)">3. 分镜生成</div>
      <div class="phase-item" data-phase="4" onclick="switchPhase(4)">4. 视频生成</div>
      <div class="phase-item" data-phase="5" onclick="switchPhase(5)">5. 配音合成</div>
      <div class="phase-item" data-phase="6" onclick="switchPhase(6)">6. 成片导出</div>
    </div>
    <div class="content" id="mainArea">
      <div class="empty"><i class="bi bi-folder2"></i>加载中...</div>
    </div>
  </div>
  <div class="chat-panel" id="chatPanel">
    <div class="chat-head"><i class="bi bi-robot"></i> AI 总控助手</div>
    <div class="chat-msgs" id="chatMsgs">
      <div class="chat-msg bot">你好！我是 AI 总控助手。可以帮你设置创作风格、优化剧本、回答创作问题。<br><br>请先选择或创建一个项目。</div>
    </div>
    <div class="chat-input">
      <input type="text" id="chatInput" placeholder="输入消息..." onkeydown="if(event.key==='Enter')sendChat()">
      <button class="chat-send" onclick="sendChat()"><i class="bi bi-send"></i></button>
    </div>
  </div>
</div>

<div class="modal-mask" id="newMask">
<div class="modal">
  <div class="modal-head"><div class="modal-title">新建项目</div><button class="modal-close" onclick="closeNew()"><i class="bi bi-x"></i></button></div>
  <div class="modal-body">
    <div class="form-group">
      <label class="form-label">项目名称</label>
      <input class="form-input" id="fName" placeholder="例如：我的漫剧项目">
    </div>
    <div class="form-group">
      <label class="form-label">小说文件</label>
      <input class="form-input" id="fFile" type="file" accept=".txt,.docx,.pdf,.epub">
      <div style="font-size:.75rem;color:#999;margin-top:4px">支持 txt/docx/pdf/epub</div>
    </div>
    <button class="btn btn-primary" onclick="doCreate()" style="width:100%"><i class="bi bi-cloud-arrow-up"></i> 导入并创建</button>
  </div>
</div>
</div>

<div class="modal-mask" id="setMask">
<div class="modal">
  <div class="modal-head"><div class="modal-title">AI 模型设置</div><button class="modal-close" onclick="closeSet()"><i class="bi bi-x"></i></button></div>
  <div class="modal-body" id="setBody">加载中...</div>
</div>
</div>

<div class="toast-box" id="toastBox"></div>

<script>
var curProj=null,chatHist=[],curPhase=1;

function $(id){return document.getElementById(id)}

function toast(msg,type){
  var d=document.createElement('div');
  d.className='toast '+(type||'');
  var icon=type==='er'?'x-circle':type==='wn'?'exclamation-circle':'check-circle';
  d.innerHTML='<i class="bi bi-'+icon+'"></i> '+msg;
  $('toastBox').appendChild(d);
  setTimeout(function(){d.remove()},3500);
}

function goHome(){
  curProj=null;curPhase=1;chatHist=[];
  $('navHome').className='nav-item on';
  $('navNew').className='nav-item';
  $('navAuto').className='nav-item';
  $('phaseNav').style.display='none';
  $('pageTitle').textContent='我的项目';
  $('pageSub').textContent='管理您的所有短剧项目';
  $('topActions').innerHTML='<button class="btn btn-primary btn-sm" onclick="openNew()"><i class="bi bi-plus-lg"></i> 新建项目</button>';
  $('chatMsgs').innerHTML='<div class="chat-msg bot">你好！我是 AI 总控助手。可以帮你设置创作风格、优化剧本、回答创作问题。<br><br>请先选择或创建一个项目。</div>';
  loadProjList();
}

function goAuto(){
  curProj=null;curPhase=0;
  $('navHome').className='nav-item';
  $('navNew').className='nav-item';
  $('navAuto').className='nav-item on';
  $('phaseNav').style.display='none';
  $('pageTitle').textContent='自动生产中心';
  $('pageSub').textContent='24小时无人值守自动生产漫剧';
  $('topActions').innerHTML='';
  loadAutoPanel();
}

function loadProjList(){
  fetch('/api/projects').then(function(r){return r.json()}).then(function(d){
    var h='';
    if(!d.projects||!d.projects.length){
      h='<div class="empty"><i class="bi bi-folder2"></i>暂无项目<br><span style="font-size:.78rem;color:#bbb">点击「新建项目」导入小说开始创作</span></div>';
      $('mainArea').innerHTML=h;
      return;
    }
    h+='<div class="grid">';
    for(var i=0;i<d.projects.length;i++){
      var p=d.projects[i];
      var s=p.stats||{};
      h+='<div class="card card-proj" data-proj="'+p.name+'">';
      h+='<div class="card-proj-body">';
      h+='<div class="card-proj-name">'+p.name+'</div>';
      h+='<div class="card-proj-meta">';
      h+='<span><i class="bi bi-file-text"></i> 剧本 '+(s.scripts||0)+'</span>';
      h+='<span><i class="bi bi-people"></i> 角色 '+(s.characters||0)+'</span>';
      h+='<span><i class="bi bi-play-circle"></i> 视频 '+(s.videos||0)+'</span>';
      h+='</div></div></div>';
    }
    h+='</div>';
    $('mainArea').innerHTML=h;
    
    var cards=document.querySelectorAll('.card-proj');
    for(var j=0;j<cards.length;j++){
      cards[j].onclick=function(){
        openProj(this.getAttribute('data-proj'));
      };
    }
  }).catch(function(){$('mainArea').innerHTML='<div class="empty">加载失败</div>'});
}

function openProj(name){
  curProj={name:name};
  curPhase=1;
  $('navHome').className='nav-item on';
  $('navAuto').className='nav-item';
  $('phaseNav').style.display='flex';
  $('pageTitle').textContent=name;
  $('pageSub').textContent='项目详情';
  $('topActions').innerHTML='<button class="btn btn-ghost btn-sm" onclick="goHome()"><i class="bi bi-arrow-left"></i> 返回</button> <button class="btn btn-danger btn-sm" id="btnDelProj"><i class="bi bi-trash3"></i> 删除</button>';
  
  $('btnDelProj').onclick=function(){
    if(confirm('确定删除项目 '+name+' ？')){
      fetch('/api/projects/'+encodeURIComponent(name)+'/delete',{method:'POST'})
      .then(function(r){return r.json()}).then(function(d){
        if(d.success){toast('已删除','ok');goHome()}
        else toast(d.error||'删除失败','er');
      }).catch(function(){toast('请求失败','er')});
    }
  };
  
  $('chatMsgs').innerHTML='<div class="chat-msg bot">已选择项目：<b>'+name+'</b><br>我可以帮你：<br>- 设置创作风格和基调<br>- 讨论角色设定<br>- 优化剧本内容<br>- 解答创作问题</div>';
  chatHist=[{role:'assistant',content:'已选择项目：'+name}];
  
  updatePhaseUI();
  loadPhase(1);
}

function switchPhase(n){
  curPhase=n;
  updatePhaseUI();
  loadPhase(n);
}

function updatePhaseUI(){
  var items=document.querySelectorAll('.phase-item');
  for(var i=0;i<items.length;i++){
    var p=parseInt(items[i].getAttribute('data-phase'));
    items[i].className='phase-item'+(p===curPhase?' on':'');
  }
}

function loadPhase(n){
  if(!curProj)return;
  var name=curProj.name;
  if(n===1) loadPhase1(name);
  else if(n===2) loadPhase2(name);
  else if(n===3) loadPhase3(name);
  else if(n===4) loadPhase4(name);
  else if(n===5) loadPhase5(name);
  else if(n===6) loadPhase6(name);
}

function loadPhase1(name){
  var h='<div class="card"><div class="card-title"><i class="bi bi-file-text"></i> 剧本管理</div>';
  h+='<div style="display:flex;gap:8px;margin-bottom:16px">';
  h+='<button class="btn btn-primary btn-sm" id="btnGenScript"><i class="bi bi-magic"></i> AI 生成剧本</button>';
  h+='<button class="btn btn-ghost btn-sm" id="btnChapters"><i class="bi bi-list-check"></i> 查看章节</button>';
  h+='</div>';
  h+='<div id="scriptList"><div class="empty">加载中...</div></div></div>';
  $('mainArea').innerHTML=h;
  
  $('btnGenScript').onclick=function(){
    toast('正在生成剧本...','');
    fetch('/api/script/generate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:name})})
    .then(function(r){return r.json()}).then(function(d){
      if(d.success)toast('剧本生成成功','ok');
      else toast(d.error||'生成失败','er');
    }).catch(function(){toast('请求失败','er')});
  };
  
  $('btnChapters').onclick=function(){
    fetch('/api/novels/'+encodeURIComponent(name)+'/chapters')
    .then(function(r){return r.json()}).then(function(d){
      var chapters=d.chapters||[];
      if(!chapters.length){toast('暂无章节数据','wn');return}
      var msg='章节列表：\n';
      for(var i=0;i<chapters.length;i++){
        msg+=(i+1)+'. '+(chapters[i].title||'第'+(i+1)+'章')+'\n';
      }
      alert(msg);
    }).catch(function(){toast('加载失败','er')});
  };
  
  fetch('/api/projects/'+encodeURIComponent(name)+'/scripts').then(function(r){return r.json()}).then(function(d){
    var scripts=d.scripts||[];
    var content='';
    if(scripts.length>0){
      for(var i=0;i<scripts.length;i++){
        var sp=scripts[i];
        content+='<div style="padding:12px;background:#f9fafb;border-radius:8px;margin-bottom:8px;border:1px solid #e5e7eb">';
        content+='<div style="display:flex;justify-content:space-between;align-items:center">';
        content+='<div><b>'+(sp.title||'剧本 '+(i+1))+'</b>';
        if(sp.style)content+=' <span class="status status-done">'+sp.style+'</span>';
        content+='</div></div>';
        if(sp.characters&&sp.characters.length){
          content+='<div style="font-size:.75rem;color:#666;margin-top:6px">角色：';
          for(var j=0;j<sp.characters.length;j++){
            content+=(j>0?'、':'')+sp.characters[j].name;
          }
          content+='</div>';
        }
        if(sp.items&&sp.items.length){
          content+='<div style="font-size:.75rem;color:#666">物品：';
          for(var k=0;k<sp.items.length;k++){
            content+=(k>0?'、':'')+sp.items[k].name;
          }
          content+='</div>';
        }
        content+='</div>';
      }
    }else{
      content='<div class="empty"><i class="bi bi-journal-text"></i>暂无剧本<br><span style="font-size:.75rem">点击「AI 生成剧本」或通过右侧 AI 助手生成</span></div>';
    }
    $('scriptList').innerHTML=content;
  }).catch(function(){$('scriptList').innerHTML='<div class="empty">加载失败</div>'});
}

function loadPhase2(name){
  var h='<div class="card"><div class="card-title"><i class="bi bi-image"></i> 资产管理</div>';
  h+='<div style="display:flex;gap:8px;margin-bottom:16px">';
  h+='<button class="btn btn-primary btn-sm" id="btnGenAssets"><i class="bi bi-magic"></i> 生成角色/场景</button>';
  h+='</div>';
  h+='<div id="assetList"><div class="empty">点击「生成」开始创建资产</div></div></div>';
  $('mainArea').innerHTML=h;
  
  $('btnGenAssets').onclick=function(){
    toast('正在生成资产...','');
    fetch('/api/assets/generate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:name})})
    .then(function(r){return r.json()}).then(function(d){
      if(d.success)toast('资产生成已启动','ok');
      else toast(d.error||'生成失败','er');
    }).catch(function(){toast('请求失败','er')});
  };
}

function loadPhase3(name){
  var h='<div class="card"><div class="card-title"><i class="bi bi-camera-reels"></i> 分镜管理</div>';
  h+='<div style="display:flex;gap:8px;margin-bottom:16px">';
  h+='<button class="btn btn-primary btn-sm" id="btnGenSB"><i class="bi bi-magic"></i> 生成分镜</button>';
  h+='</div>';
  h+='<div id="sbList"><div class="empty">点击「生成」开始创建分镜</div></div></div>';
  $('mainArea').innerHTML=h;
  
  $('btnGenSB').onclick=function(){
    toast('正在生成分镜...','');
    fetch('/api/storyboards/generate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:name})})
    .then(function(r){return r.json()}).then(function(d){
      if(d.success)toast('分镜生成已启动','ok');
      else toast(d.error||'生成失败','er');
    }).catch(function(){toast('请求失败','er')});
  };
}

function loadPhase4(name){
  var h='<div class="card"><div class="card-title"><i class="bi bi-play-circle"></i> 视频生成</div>';
  h+='<div style="display:flex;gap:8px;margin-bottom:16px">';
  h+='<button class="btn btn-primary btn-sm" id="btnGenVid"><i class="bi bi-magic"></i> 生成视频</button>';
  h+='</div>';
  h+='<div id="vidList"><div class="empty">点击「生成」开始创建视频</div></div></div>';
  $('mainArea').innerHTML=h;
  
  $('btnGenVid').onclick=function(){
    toast('正在生成视频...','');
    fetch('/api/videos/generate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:name})})
    .then(function(r){return r.json()}).then(function(d){
      if(d.success)toast('视频生成已启动','ok');
      else toast(d.error||'生成失败','er');
    }).catch(function(){toast('请求失败','er')});
  };
}

function loadPhase5(name){
  var h='<div class="card"><div class="card-title"><i class="bi bi-mic"></i> 配音合成</div>';
  h+='<div style="display:flex;gap:8px;margin-bottom:16px">';
  h+='<button class="btn btn-primary btn-sm" id="btnGenDub"><i class="bi bi-magic"></i> 生成配音</button>';
  h+='<button class="btn btn-primary btn-sm" id="btnGenMix"><i class="bi bi-music-note-beamed"></i> 音画合成</button>';
  h+='</div>';
  h+='<div id="dubList"><div class="empty">点击「生成」开始配音</div></div></div>';
  $('mainArea').innerHTML=h;
  
  $('btnGenDub').onclick=function(){
    toast('正在生成配音...','');
    fetch('/api/tts/generate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:name})})
    .then(function(r){return r.json()}).then(function(d){
      if(d.success)toast('配音生成已启动','ok');
      else toast(d.error||'生成失败','er');
    }).catch(function(){toast('请求失败','er')});
  };
  
  $('btnGenMix').onclick=function(){
    toast('正在音画合成...','');
    fetch('/api/mix/generate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:name})})
    .then(function(r){return r.json()}).then(function(d){
      if(d.success)toast('合成已启动','ok');
      else toast(d.error||'合成失败','er');
    }).catch(function(){toast('请求失败','er')});
  };
}

function loadPhase6(name){
  var h='<div class="card"><div class="card-title"><i class="bi bi-film"></i> 成片导出</div>';
  h+='<div style="display:flex;gap:8px;margin-bottom:16px">';
  h+='<button class="btn btn-primary btn-sm" id="btnGenFinal"><i class="bi bi-magic"></i> 合成成片</button>';
  h+='</div>';
  h+='<div id="finalList"><div class="empty">点击「合成」生成最终成片</div></div></div>';
  $('mainArea').innerHTML=h;
  
  $('btnGenFinal').onclick=function(){
    toast('正在合成成片...','');
    fetch('/api/final/video',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:name})})
    .then(function(r){return r.json()}).then(function(d){
      if(d.success)toast('成片合成已启动','ok');
      else toast(d.error||'合成失败','er');
    }).catch(function(){toast('请求失败','er')});
  };
}

function loadAutoPanel(){
  var h='<div class="auto-panel">';
  h+='<h3><i class="bi bi-robot"></i> 24小时自动生产</h3>';
  h+='<div class="auto-stats">';
  h+='<div class="auto-stat"><div class="auto-stat-val" id="autoRunning">0</div><div class="auto-stat-label">生产中</div></div>';
  h+='<div class="auto-stat"><div class="auto-stat-val" id="autoDone">0</div><div class="auto-stat-label">已完成</div></div>';
  h+='<div class="auto-stat"><div class="auto-stat-val" id="autoFail">0</div><div class="auto-stat-label">异常</div></div>';
  h+='<div class="auto-stat"><div class="auto-stat-val" id="autoQueue">0</div><div class="auto-stat-label">队列</div></div>';
  h+='</div>';
  h+='<div style="display:flex;gap:8px">';
  h+='<button class="btn btn-primary" id="btnEnableAuto"><i class="bi bi-play-fill"></i> 启动自动生产</button>';
  h+='<button class="btn btn-ghost" style="color:#fff;border-color:rgba(255,255,255,.3)" id="btnDisableAuto"><i class="bi bi-pause-fill"></i> 暂停</button>';
  h+='</div></div>';
  h+='<div class="card"><div class="card-title"><i class="bi bi-list-task"></i> 生产进度</div>';
  h+='<div class="prod-list" id="autoProgress"><div class="empty">暂无项目在生产</div></div></div>';
  $('mainArea').innerHTML=h;
  
  $('btnEnableAuto').onclick=function(){
    fetch('/api/autopilot/enable',{method:'POST'}).then(function(r){return r.json()}).then(function(d){
      if(d.success)toast('自动生产已启动','ok');
      else toast(d.error||'启动失败','er');
    }).catch(function(){toast('请求失败','er')});
  };
  
  $('btnDisableAuto').onclick=function(){
    fetch('/api/autopilot/disable',{method:'POST'}).then(function(r){return r.json()}).then(function(d){
      if(d.success)toast('自动生产已暂停','ok');
      else toast(d.error||'暂停失败','er');
    }).catch(function(){toast('请求失败','er')});
  };
  
  loadAutoStatus();
}

function loadAutoStatus(){
  fetch('/api/autopilot/status').then(function(r){return r.json()}).then(function(d){
    $('autoRunning').textContent=d.running||0;
    $('autoDone').textContent=d.completed||0;
    $('autoFail').textContent=d.exceptions||0;
    $('autoQueue').textContent=d.queued||0;
  }).catch(function(){});
  
  fetch('/api/autopilot/progress').then(function(r){return r.json()}).then(function(d){
    var progs=d.projects||[];
    if(!progs.length){$('autoProgress').innerHTML='<div class="empty">暂无项目在生产</div>';return}
    var h='';
    for(var i=0;i<progs.length;i++){
      var p=progs[i];
      var pct=p.total?Math.round(p.done/p.total*100):0;
      h+='<div class="prod-item">';
      h+='<div class="prod-item-name">'+p.name+'</div>';
      h+='<div class="prod-item-progress"><div class="progress"><div class="progress-bar" style="width:'+pct+'%"></div></div><div style="font-size:.72rem;color:#666">'+(p.done||0)+'/'+(p.total||0)+' 集</div></div>';
      h+='<div class="prod-item-status"><span class="status status-'+(p.status==='running'?'running':p.status==='done'?'done':'wait')+'">'+p.status+'</span></div>';
      h+='</div>';
    }
    $('autoProgress').innerHTML=h;
  }).catch(function(){$('autoProgress').innerHTML='<div class="empty">加载失败</div>'});
}

function openNew(){$('newMask').className='modal-mask on';$('navNew').className='nav-item on'}
function closeNew(){$('newMask').className='modal-mask';$('navNew').className='nav-item'}

function doCreate(){
  var name=$('fName').value.trim();
  var file=$('fFile').files[0];
  if(!name){toast('请输入项目名称','wn');return}
  if(!file){toast('请选择小说文件','wn');return}
  toast('正在导入...','');
  var fd=new FormData();
  fd.append('file',file);
  fd.append('project_name',name);
  fetch('/api/novels/upload',{method:'POST',body:fd})
  .then(function(r){return r.json()}).then(function(d){
    if(d.success||d.ok){toast('导入成功','ok');closeNew();loadProjList()}
    else toast(d.error||'导入失败','er');
  }).catch(function(){toast('请求失败','er')});
}

function sendChat(){
  var input=$('chatInput');
  var msg=input.value.trim();
  if(!msg)return;
  input.value='';
  
  var userDiv=document.createElement('div');
  userDiv.className='chat-msg user';
  userDiv.textContent=msg;
  $('chatMsgs').appendChild(userDiv);
  chatHist.push({role:'user',content:msg});
  $('chatMsgs').scrollTop=$('chatMsgs').scrollHeight;
  
  fetch('/api/ai/chat',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message:msg,project:curProj?curProj.name:null,history:chatHist})})
  .then(function(r){return r.json()}).then(function(d){
    var reply=d.reply||d.response||d.message||'抱歉，无法回复';
    var botDiv=document.createElement('div');
    botDiv.className='chat-msg bot';
    botDiv.textContent=reply;
    $('chatMsgs').appendChild(botDiv);
    chatHist.push({role:'assistant',content:reply});
    $('chatMsgs').scrollTop=$('chatMsgs').scrollHeight;
  }).catch(function(){
    var errDiv=document.createElement('div');
    errDiv.className='chat-msg bot';
    errDiv.style.color='#ef4444';
    errDiv.textContent='请求失败，请检查网络';
    $('chatMsgs').appendChild(errDiv);
  });
}

function openSet(){
  $('setMask').className='modal-mask on';
  $('setBody').innerHTML='加载中...';
  fetch('/api/ai/config').then(function(r){return r.json()}).then(function(d){
    var h='';
    var modules=['text','qc','chat'];
    var labels={text:'文本分析模型',qc:'质检模型',chat:'对话总控模型'};
    for(var i=0;i<modules.length;i++){
      var m=modules[i];
      var c=d[m]||{};
      h+='<div style="margin-bottom:16px;padding:12px;background:#f9fafb;border-radius:8px">';
      h+='<div style="font-weight:600;margin-bottom:10px">'+labels[m]+'</div>';
      h+='<div class="form-group"><label class="form-label">API Base URL</label><input class="form-input" id="set_'+m+'_url" value="'+(c.base_url||'')+'"></div>';
      h+='<div class="form-group"><label class="form-label">Model</label><input class="form-input" id="set_'+m+'_model" value="'+(c.model||'')+'"></div>';
      h+='<div class="form-group"><label class="form-label">API Key</label><input class="form-input" id="set_'+m+'_key" type="password" value="'+(c.api_key||'')+'"></div>';
      h+='<button class="btn btn-ghost btn-sm" data-module="'+m+'">保存</button>';
      h+='</div>';
    }
    $('setBody').innerHTML=h;
    
    var btns=$('setBody').querySelectorAll('button[data-module]');
    for(var j=0;j<btns.length;j++){
      btns[j].onclick=function(){
        var mod=this.getAttribute('data-module');
        var base_url=$('set_'+mod+'_url').value.trim();
        var model=$('set_'+mod+'_model').value.trim();
        var api_key=$('set_'+mod+'_key').value.trim();
        fetch('/api/ai/config',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({module:mod,base_url:base_url,model:model,api_key:api_key})})
        .then(function(r){return r.json()}).then(function(d2){
          if(d2.success)toast('保存成功','ok');
          else toast(d2.error||'保存失败','er');
        }).catch(function(){toast('请求失败','er')});
      };
    }
  }).catch(function(){$('setBody').innerHTML='<div class="empty">加载失败</div>'});
}

function closeSet(){$('setMask').className='modal-mask'}

goHome();
</script>
</body>
</html>"""

def build():
    out = os.path.join(os.path.dirname(__file__), 'index.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(HTML)
    print(f'Written {os.path.getsize(out)} bytes to {out}')

if __name__ == '__main__':
    build()
