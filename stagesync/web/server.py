"""StageSync Web Dashboard & REST Server.

Provides a modern, lightweight web interface to monitor video discovery,
review identified concert footage, inspect setlists, and approve YouTube uploads.
Redesigned with a Figma Task & Project Management Dashboard layout (Kanban boards,
review triage queue, catalog data grid, and slide-over inspector drawer).
Powered by aiohttp.web with zero node/npm build dependencies.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional
import webbrowser

from aiohttp import web

from stagesync.config import Settings, get_settings
from stagesync.database.connection import Database, get_db
from stagesync.database.models import PrivacyStatus, VideoStatus
from stagesync.pipeline import PipelineCoordinator

logger = logging.getLogger(__name__)

SETTINGS_KEY = web.AppKey("settings", Settings)
DB_KEY = web.AppKey("db", Database)
COORD_KEY = web.AppKey("coordinator", PipelineCoordinator)

HTML_PAGE = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>StageSync | Task & Project Management Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          fontFamily: {
            sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'sans-serif'],
            mono: ['"JetBrains Mono"', 'monospace']
          },
          colors: {
            brand: {
              50: '#eef2ff',
              100: '#e0e7ff',
              200: '#c7d2fe',
              500: '#6366f1',
              600: '#4f46e5',
              700: '#4338ca'
            },
            canvas: '#090d16',
            card: {
              DEFAULT: '#111726',
              hover: '#161f33',
              subtle: '#0d1320'
            },
            border: {
              DEFAULT: '#1e293b',
              subtle: '#172033'
            }
          }
        }
      }
    }
  </script>
  <style>
    body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .tabular { font-variant-numeric: tabular-nums; }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #090d16; }
    ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 9999px; }
    ::-webkit-scrollbar-thumb:hover { background: #334155; }
    .drawer-overlay {
      transition: opacity 0.25s ease-in-out;
    }
    .drawer-panel {
      transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);
    }
  </style>
</head>
<body class="bg-canvas text-zinc-100 min-h-screen antialiased flex overflow-x-hidden selection:bg-brand-500 selection:text-white">

  <!-- Desktop & Mobile Sidebar Navigation -->
  <aside id="app-sidebar" class="w-64 bg-card border-r border-border flex flex-col fixed inset-y-0 left-0 z-40 transition-transform duration-200 -translate-x-full lg:translate-x-0">
    
    <!-- Workspace Brand Header -->
    <div class="h-16 border-b border-border px-5 flex items-center justify-between">
      <div class="flex items-center gap-3">
        <div class="w-9 h-9 rounded-xl bg-gradient-to-tr from-brand-600 via-indigo-500 to-blue-500 flex items-center justify-center font-bold text-white shadow-md shadow-brand-500/20">
          <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3" />
          </svg>
        </div>
        <div class="leading-tight">
          <div class="font-bold text-sm tracking-tight text-white flex items-center gap-1.5">
            StageSync
            <span class="text-[10px] font-mono px-1.5 py-0.2 bg-brand-500/10 text-brand-400 border border-brand-500/20 rounded">PRO</span>
          </div>
          <div class="text-[11px] text-zinc-400">Task & Video Vault</div>
        </div>
      </div>
      <button onclick="toggleSidebar()" class="lg:hidden text-zinc-400 hover:text-white p-1">
        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
      </button>
    </div>

    <!-- Project Workspace Switcher Dropdown (Figma Style) -->
    <div class="p-3">
      <div class="bg-card-subtle border border-border rounded-xl p-2.5 flex items-center justify-between text-xs cursor-pointer hover:border-zinc-700 transition">
        <div class="flex items-center gap-2 overflow-hidden">
          <span class="w-2 h-2 rounded-full bg-emerald-500 shadow-sm shadow-emerald-500/50 flex-shrink-0"></span>
          <span class="font-medium text-zinc-200 truncate">Live Concerts Vault</span>
        </div>
        <svg class="w-4 h-4 text-zinc-400 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l4 4 4-4" />
        </svg>
      </div>
    </div>

    <!-- Navigation Menu Items -->
    <div class="flex-1 px-3 py-2 space-y-6 overflow-y-auto">
      <div>
        <div class="px-3 pb-2 text-[10px] font-semibold text-zinc-400 tracking-wider uppercase">Project Views</div>
        <nav class="space-y-1">
          <button onclick="setTab('board')" id="nav-btn-board" class="w-full flex items-center justify-between px-3 py-2 rounded-lg text-xs font-medium transition text-zinc-300 hover:text-white hover:bg-zinc-800/50">
            <div class="flex items-center gap-2.5">
              <svg class="w-4 h-4 text-zinc-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 17V7m0 10a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2h2a2 2 0 012 2m0 10a2 2 0 002 2h2a2 2 0 002-2M9 7a2 2 0 012-2h2a2 2 0 012 2m0 10V7m0 10a2 2 0 002 2h2a2 2 0 002-2V7a2 2 0 00-2-2h-2a2 2 0 00-2 2" /></svg>
              <span>Kanban Board</span>
            </div>
            <span id="nav-count-board" class="text-[10px] font-mono px-1.5 py-0.5 rounded-full bg-zinc-800 text-zinc-400">0</span>
          </button>

          <button onclick="setTab('review')" id="nav-btn-review" class="w-full flex items-center justify-between px-3 py-2 rounded-lg text-xs font-medium transition text-zinc-300 hover:text-white hover:bg-zinc-800/50">
            <div class="flex items-center gap-2.5">
              <svg class="w-4 h-4 text-zinc-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" /></svg>
              <span>Review Queue</span>
            </div>
            <span id="nav-count-review" class="text-[10px] font-mono px-1.5 py-0.5 rounded-full bg-amber-500/10 text-amber-400 border border-amber-500/20">0</span>
          </button>

          <button onclick="setTab('catalog')" id="nav-btn-catalog" class="w-full flex items-center justify-between px-3 py-2 rounded-lg text-xs font-medium transition text-zinc-300 hover:text-white hover:bg-zinc-800/50">
            <div class="flex items-center gap-2.5">
              <svg class="w-4 h-4 text-zinc-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 10h18M3 14h18m-9-4v8m-7 0h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" /></svg>
              <span>Video Catalog</span>
            </div>
            <span id="nav-count-catalog" class="text-[10px] font-mono px-1.5 py-0.5 rounded-full bg-zinc-800 text-zinc-400">0</span>
          </button>

          <button onclick="setTab('overview')" id="nav-btn-overview" class="w-full flex items-center justify-between px-3 py-2 rounded-lg text-xs font-medium transition text-zinc-300 hover:text-white hover:bg-zinc-800/50">
            <div class="flex items-center gap-2.5">
              <svg class="w-4 h-4 text-zinc-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" /></svg>
              <span>Overview & KPIs</span>
            </div>
          </button>
        </nav>
      </div>

      <div>
        <div class="px-3 pb-2 text-[10px] font-semibold text-zinc-400 tracking-wider uppercase">Pipeline Sagas</div>
        <div class="space-y-1">
          <button onclick="openScanModal()" class="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-xs font-medium text-zinc-300 hover:text-white hover:bg-zinc-800/50 transition">
            <svg class="w-4 h-4 text-indigo-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0zM10 7v3m0 0v3m0-3h3m-3 0H7" /></svg>
            <span>Scan Discovery</span>
          </button>
          <button onclick="triggerAction('process')" id="sidebar-btn-process" class="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-xs font-medium text-zinc-300 hover:text-white hover:bg-zinc-800/50 transition">
            <svg class="w-4 h-4 text-amber-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>
            <span>Run AI Matcher</span>
          </button>
          <button onclick="triggerAction('upload')" id="sidebar-btn-upload" class="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-xs font-medium text-zinc-300 hover:text-white hover:bg-zinc-800/50 transition">
            <svg class="w-4 h-4 text-blue-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" /></svg>
            <span>Publish to YouTube</span>
          </button>
        </div>
      </div>
    </div>

    <!-- Sidebar Bottom / Quota & Engine Stats -->
    <div class="p-3 border-t border-border bg-card-subtle">
      <div class="bg-canvas border border-border rounded-xl p-3 space-y-2">
        <div class="flex items-center justify-between text-[11px]">
          <span class="text-zinc-400 font-medium">Daily Upload Quota</span>
          <span id="sidebar-quota-text" class="text-zinc-200 font-mono font-semibold tabular">0 / 5</span>
        </div>
        <div class="w-full bg-zinc-800 rounded-full h-1.5 overflow-hidden">
          <div id="sidebar-quota-bar" class="bg-gradient-to-r from-blue-500 to-indigo-500 h-1.5 rounded-full transition-all duration-300" style="width: 0%"></div>
        </div>
        <div class="pt-1 flex items-center justify-between text-[10px] text-zinc-400 border-t border-border/40">
          <span class="flex items-center gap-1.5">
            <span class="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></span>
            SQLite WAL
          </span>
          <span class="font-mono">v0.1.0</span>
        </div>
      </div>
    </div>
  </aside>

  <!-- Mobile Backdrop -->
  <button type="button" id="sidebar-backdrop" aria-label="Close sidebar navigation" onclick="toggleSidebar()" class="fixed inset-0 bg-black/60 backdrop-blur-sm z-30 hidden lg:hidden w-full h-full border-0 cursor-default"></button>

  <!-- Main Workspace Content Frame -->
  <div class="lg:pl-64 flex flex-col flex-1 min-w-0">

    <!-- Top Action & Navigation Bar -->
    <header class="h-16 border-b border-border bg-canvas/90 backdrop-blur sticky top-0 z-20 px-4 sm:px-6 flex items-center justify-between gap-4">
      <div class="flex items-center gap-3">
        <button onclick="toggleSidebar()" class="lg:hidden text-zinc-400 hover:text-white p-1 rounded-md hover:bg-zinc-800">
          <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" /></svg>
        </button>
        <div>
          <div class="flex items-center gap-2 text-xs text-zinc-400">
            <span>Workspaces</span>
            <span>/</span>
            <span>StageSync</span>
            <span>/</span>
            <span id="breadcrumb-active-view" class="text-zinc-200 font-medium">Kanban Board</span>
          </div>
          <h1 id="page-title" class="text-sm sm:text-base font-bold text-white tracking-tight flex items-center gap-2">
            Task Management Pipeline
          </h1>
        </div>
      </div>

      <!-- Center Live Filter / Search Input -->
      <div class="hidden md:flex items-center flex-1 max-w-md mx-4">
        <div class="relative w-full">
          <span class="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none text-zinc-400">
            <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" /></svg>
          </span>
          <input type="text" id="global-search" oninput="onSearchInput(this.value)" placeholder="Search track, artist, venue, or filename (Press '/' or ⌘K)..." class="w-full bg-card border border-border rounded-xl pl-9 pr-12 py-1.5 text-xs text-zinc-200 placeholder-zinc-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:border-brand-500 transition">
          <span class="absolute inset-y-0 right-0 pr-2.5 flex items-center pointer-events-none text-[10px] text-zinc-400 font-mono">
            ⌘K
          </span>
        </div>
      </div>

      <!-- Action Button Toolbar -->
      <div class="flex items-center gap-2">
        <button onclick="loadData()" title="Refresh Data" class="p-2 rounded-xl bg-card border border-border hover:bg-card-hover text-zinc-300 transition">
          <svg id="refresh-icon" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
        </button>

        <button onclick="openScanModal()" class="px-3 py-1.5 text-xs font-semibold rounded-xl bg-card border border-border hover:bg-card-hover text-zinc-200 shadow-sm transition flex items-center gap-1.5">
          <svg class="w-3.5 h-3.5 text-indigo-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" /></svg>
          <span>Scan Media</span>
        </button>

        <button onclick="triggerAction('upload')" id="top-btn-upload" class="px-3.5 py-1.5 text-xs font-semibold rounded-xl bg-gradient-to-r from-brand-600 to-indigo-600 hover:from-brand-500 hover:to-indigo-500 text-white shadow-md shadow-brand-500/20 transition flex items-center gap-1.5">
          <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" /></svg>
          <span>Publish (<span id="top-approved-count">0</span>)</span>
        </button>
      </div>
    </header>

    <!-- Sub-Header: Active Tab Switcher & Status Filter Bar -->
    <div class="border-b border-border bg-card/40 px-4 sm:px-6 py-2.5 flex flex-wrap items-center justify-between gap-3 text-xs">
      <!-- View Mode Buttons -->
      <div class="flex items-center gap-1 bg-canvas p-1 rounded-xl border border-border">
        <button onclick="setTab('board')" id="tab-btn-board" class="px-3 py-1 rounded-lg font-medium transition flex items-center gap-1.5">
          <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 17V7m0 10a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2h2a2 2 0 012 2m0 10a2 2 0 002 2h2a2 2 0 002-2M9 7a2 2 0 012-2h2a2 2 0 012 2m0 10V7m0 10a2 2 0 002 2h2a2 2 0 002-2V7a2 2 0 00-2-2h-2a2 2 0 00-2 2" /></svg>
          Board
        </button>
        <button onclick="setTab('review')" id="tab-btn-review" class="px-3 py-1 rounded-lg font-medium transition flex items-center gap-1.5">
          <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" /></svg>
          Review Queue (<span id="tab-count-review">0</span>)
        </button>
        <button onclick="setTab('catalog')" id="tab-btn-catalog" class="px-3 py-1 rounded-lg font-medium transition flex items-center gap-1.5">
          <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 10h18M3 14h18m-9-4v8m-7 0h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" /></svg>
          Catalog Table (<span id="tab-count-catalog">0</span>)
        </button>
        <button onclick="setTab('overview')" id="tab-btn-overview" class="px-3 py-1 rounded-lg font-medium transition flex items-center gap-1.5">
          <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" /></svg>
          Overview
        </button>
      </div>

      <!-- Quick Filter Pills -->
      <div class="flex items-center gap-1.5 overflow-x-auto py-1">
        <span class="text-[11px] text-zinc-400 mr-1">Filter:</span>
        <button onclick="setStatusFilter('ALL')" id="filter-btn-ALL" class="px-2.5 py-1 rounded-lg border text-[11px] font-medium transition bg-zinc-800 text-white border-zinc-700">All</button>
        <button onclick="setStatusFilter('PENDING_REVIEW')" id="filter-btn-PENDING_REVIEW" class="px-2.5 py-1 rounded-lg border text-[11px] font-medium transition bg-card border-border text-zinc-400 hover:text-zinc-200">Needs Review</button>
        <button onclick="setStatusFilter('APPROVED')" id="filter-btn-APPROVED" class="px-2.5 py-1 rounded-lg border text-[11px] font-medium transition bg-card border-border text-zinc-400 hover:text-zinc-200">Approved</button>
        <button onclick="setStatusFilter('PUBLISHED')" id="filter-btn-PUBLISHED" class="px-2.5 py-1 rounded-lg border text-[11px] font-medium transition bg-card border-border text-zinc-400 hover:text-zinc-200">Published</button>
        <button onclick="setStatusFilter('HIGH_CONF')" id="filter-btn-HIGH_CONF" class="px-2.5 py-1 rounded-lg border text-[11px] font-medium transition bg-card border-border text-zinc-400 hover:text-zinc-200">High Conf (≥90%)</button>
      </div>
    </div>

    <!-- Main Workspace Container -->
    <main class="flex-1 p-4 sm:p-6 overflow-y-auto space-y-6">

      <!-- ============================================================= -->
      <!-- VIEW 1: KANBAN BOARD VIEW                                     -->
      <!-- ============================================================= -->
      <section id="view-board" class="space-y-4">
        <!-- 4-Column Pipeline Kanban -->
        <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4 items-start">

          <!-- Column 1: Discovered -->
          <div class="bg-card/70 border border-border rounded-2xl flex flex-col max-h-[calc(100vh-190px)]">
            <div class="p-3.5 border-b border-border flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="w-2.5 h-2.5 rounded-full bg-zinc-500"></span>
                <span class="font-bold text-xs uppercase tracking-wider text-zinc-300">Discovered</span>
              </div>
              <span id="board-count-discovered" class="text-xs font-mono px-2 py-0.5 rounded-full bg-zinc-800 text-zinc-400 tabular font-semibold">0</span>
            </div>
            <div id="board-col-discovered" class="p-3 space-y-3 overflow-y-auto flex-1 min-h-[140px]">
              <!-- Cards injected via JS -->
            </div>
          </div>

          <!-- Column 2: Pending Review -->
          <div class="bg-card/70 border border-border rounded-2xl flex flex-col max-h-[calc(100vh-190px)]">
            <div class="p-3.5 border-b border-border flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="w-2.5 h-2.5 rounded-full bg-amber-400 animate-pulse"></span>
                <span class="font-bold text-xs uppercase tracking-wider text-amber-300">Review Needed</span>
              </div>
              <span id="board-count-review" class="text-xs font-mono px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-400 border border-amber-500/20 tabular font-semibold">0</span>
            </div>
            <div id="board-col-review" class="p-3 space-y-3 overflow-y-auto flex-1 min-h-[140px]">
              <!-- Cards injected via JS -->
            </div>
          </div>

          <!-- Column 3: Approved (Queued for Upload) -->
          <div class="bg-card/70 border border-border rounded-2xl flex flex-col max-h-[calc(100vh-190px)]">
            <div class="p-3.5 border-b border-border flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="w-2.5 h-2.5 rounded-full bg-indigo-500"></span>
                <span class="font-bold text-xs uppercase tracking-wider text-indigo-300">Approved & Queued</span>
              </div>
              <span id="board-count-approved" class="text-xs font-mono px-2 py-0.5 rounded-full bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 tabular font-semibold">0</span>
            </div>
            <div id="board-col-approved" class="p-3 space-y-3 overflow-y-auto flex-1 min-h-[140px]">
              <!-- Cards injected via JS -->
            </div>
          </div>

          <!-- Column 4: Published to YouTube -->
          <div class="bg-card/70 border border-border rounded-2xl flex flex-col max-h-[calc(100vh-190px)]">
            <div class="p-3.5 border-b border-border flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="w-2.5 h-2.5 rounded-full bg-emerald-500"></span>
                <span class="font-bold text-xs uppercase tracking-wider text-emerald-300">Published</span>
              </div>
              <span id="board-count-published" class="text-xs font-mono px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 tabular font-semibold">0</span>
            </div>
            <div id="board-col-published" class="p-3 space-y-3 overflow-y-auto flex-1 min-h-[140px]">
              <!-- Cards injected via JS -->
            </div>
          </div>

        </div>
      </section>

      <!-- ============================================================= -->
      <!-- VIEW 2: REVIEW QUEUE (TRIAGE CARDS)                           -->
      <!-- ============================================================= -->
      <section id="view-review" class="hidden space-y-4">
        <div id="review-list" class="grid grid-cols-1 gap-4">
          <div class="text-center py-16 text-zinc-400 text-sm">Loading review queue...</div>
        </div>
      </section>

      <!-- ============================================================= -->
      <!-- VIEW 3: FULL CATALOG (DATA TABLE)                             -->
      <!-- ============================================================= -->
      <section id="view-catalog" class="hidden space-y-4">
        <div class="bg-card border border-border rounded-2xl overflow-hidden shadow-lg">
          <div class="px-5 py-3.5 border-b border-border flex items-center justify-between text-xs">
            <span class="font-semibold text-zinc-300">Video Master Inventory</span>
            <span class="text-zinc-400 font-mono" id="catalog-count-label">0 items</span>
          </div>
          <div class="overflow-x-auto">
            <table class="w-full text-left text-xs">
              <thead class="bg-card-subtle text-zinc-400 uppercase font-semibold border-b border-border text-[11px] tracking-wider">
                <tr>
                  <th class="px-5 py-3">Video File</th>
                  <th class="px-5 py-3">Artist & Track</th>
                  <th class="px-5 py-3">Venue / Location</th>
                  <th class="px-5 py-3">Confidence</th>
                  <th class="px-5 py-3">Status</th>
                  <th class="px-5 py-3 text-right">Actions</th>
                </tr>
              </thead>
              <tbody id="catalog-table-body" class="divide-y divide-border/60">
                <tr><td colspan="6" class="text-center py-12 text-zinc-400">Loading catalog...</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ============================================================= -->
      <!-- VIEW 4: SYSTEM OVERVIEW & KPIS                                -->
      <!-- ============================================================= -->
      <section id="view-overview" class="hidden space-y-6">
        <!-- 4 Metric KPI Cards -->
        <div class="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <div class="bg-card border border-border rounded-2xl p-4 sm:p-5 relative overflow-hidden group hover:border-zinc-700 transition">
            <div class="flex items-center justify-between text-zinc-400 text-xs font-medium">
              <span>Total Cataloged</span>
              <span class="p-1.5 rounded-lg bg-zinc-800 text-zinc-300">
                <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" /></svg>
              </span>
            </div>
            <div class="text-3xl font-extrabold tabular text-white mt-2" id="stat-total">0</div>
            <div class="text-[11px] text-zinc-400 mt-1 flex items-center gap-1">
              <span>All discovered concert footage</span>
            </div>
          </div>

          <div class="bg-card border border-border rounded-2xl p-4 sm:p-5 relative overflow-hidden group hover:border-amber-500/40 transition">
            <div class="flex items-center justify-between text-amber-400 text-xs font-medium">
              <span>Pending Review</span>
              <span class="p-1.5 rounded-lg bg-amber-500/10 text-amber-400 border border-amber-500/20">
                <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" /></svg>
              </span>
            </div>
            <div class="text-3xl font-extrabold tabular text-amber-400 mt-2" id="stat-pending">0</div>
            <div class="text-[11px] text-zinc-400 mt-1">Awaiting verification triage</div>
          </div>

          <div class="bg-card border border-border rounded-2xl p-4 sm:p-5 relative overflow-hidden group hover:border-indigo-500/40 transition">
            <div class="flex items-center justify-between text-indigo-400 text-xs font-medium">
              <span>Approved & Queued</span>
              <span class="p-1.5 rounded-lg bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
              </span>
            </div>
            <div class="text-3xl font-extrabold tabular text-indigo-400 mt-2" id="stat-approved">0</div>
            <div class="text-[11px] text-zinc-400 mt-1">Ready for YouTube pipeline</div>
          </div>

          <div class="bg-card border border-border rounded-2xl p-4 sm:p-5 relative overflow-hidden group hover:border-emerald-500/40 transition">
            <div class="flex items-center justify-between text-emerald-400 text-xs font-medium">
              <span>Published Live</span>
              <span class="p-1.5 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" /></svg>
              </span>
            </div>
            <div class="text-3xl font-extrabold tabular text-emerald-400 mt-2" id="stat-published">0</div>
            <div class="text-[11px] text-zinc-400 mt-1">Active on YouTube channel</div>
          </div>
        </div>

        <!-- Integration Status Cards -->
        <div class="bg-card border border-border rounded-2xl p-5 space-y-4">
          <div class="flex items-center justify-between border-b border-border pb-3">
            <div>
              <h3 class="font-bold text-sm text-zinc-100 flex items-center gap-2">
                <svg class="w-4 h-4 text-brand-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>
                Cloud Services & API Integrations Health
              </h3>
              <p class="text-xs text-zinc-400 mt-0.5">Real-time status of storage, intelligence, and publication endpoints</p>
            </div>
            <span class="text-xs font-mono text-zinc-400" id="text-quota">Daily Quota: 0 / 5</span>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3 text-xs">
            <!-- Google Drive -->
            <div class="bg-canvas border border-border rounded-xl p-3 space-y-1.5">
              <div class="flex items-center justify-between">
                <span class="font-semibold text-zinc-200">Google Drive</span>
                <span class="w-2 h-2 rounded-full" id="dot-drive"></span>
              </div>
              <div id="text-drive" class="text-xs text-zinc-400">Checking...</div>
              <div class="text-[10px] text-zinc-400">Full date & folder search</div>
            </div>

            <!-- Google Photos -->
            <div class="bg-canvas border border-border rounded-xl p-3 space-y-1.5">
              <div class="flex items-center justify-between">
                <span class="font-semibold text-zinc-200">Google Photos</span>
                <span class="w-2 h-2 rounded-full" id="dot-photos"></span>
              </div>
              <div id="text-photos" class="text-xs text-zinc-400">Checking...</div>
              <div class="text-[10px] text-zinc-400">Library & Picker API</div>
            </div>

            <!-- YouTube Data API -->
            <div class="bg-canvas border border-border rounded-xl p-3 space-y-1.5">
              <div class="flex items-center justify-between">
                <span class="font-semibold text-zinc-200">YouTube v3</span>
                <span class="w-2 h-2 rounded-full" id="dot-yt"></span>
              </div>
              <div id="text-yt" class="text-xs text-zinc-400">Checking...</div>
              <div class="text-[10px] text-zinc-400">Resumable chunk uploader</div>
            </div>

            <!-- Setlist.fm -->
            <div class="bg-canvas border border-border rounded-xl p-3 space-y-1.5">
              <div class="flex items-center justify-between">
                <span class="font-semibold text-zinc-200">Setlist.fm API</span>
                <span class="w-2 h-2 rounded-full" id="dot-setlist"></span>
              </div>
              <div id="text-setlist" class="text-xs text-zinc-400">Checking...</div>
              <div class="text-[10px] text-zinc-400">Tour & setlist discovery</div>
            </div>

            <!-- Gemini 2.5 Audio -->
            <div class="bg-canvas border border-border rounded-xl p-3 space-y-1.5">
              <div class="flex items-center justify-between">
                <span class="font-semibold text-zinc-200">Gemini Audio</span>
                <span class="w-2 h-2 rounded-full" id="dot-gemini"></span>
              </div>
              <div id="text-gemini" class="text-xs text-zinc-400">Checking...</div>
              <div class="text-[10px] text-zinc-400">Live acoustic reasoning</div>
            </div>
          </div>
        </div>

        <!-- Quick Automation Actions Bar -->
        <div class="bg-card border border-border rounded-2xl p-5 flex flex-wrap items-center justify-between gap-4">
          <div>
            <div class="font-bold text-sm text-zinc-200">Autonomous Pipeline Actions</div>
            <div class="text-xs text-zinc-400 mt-0.5">Execute batch operations across your video repository</div>
          </div>
          <div class="flex items-center gap-2">
            <button onclick="openScanModal()" class="px-3.5 py-2 text-xs font-semibold rounded-xl bg-card-subtle hover:bg-zinc-800 text-zinc-200 border border-border transition flex items-center gap-2">
              <svg class="w-4 h-4 text-indigo-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0zM10 7v3m0 0v3m0-3h3m-3 0H7" /></svg>
              <span>Scan Media Sources</span>
            </button>
            <button onclick="triggerAction('process')" id="btn-process-action" class="px-3.5 py-2 text-xs font-semibold rounded-xl bg-card-subtle hover:bg-zinc-800 text-zinc-200 border border-border transition flex items-center gap-2">
              <svg class="w-4 h-4 text-amber-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>
              <span>Process Pending Clips</span>
            </button>
            <button onclick="triggerAction('upload')" id="btn-upload-action" class="px-4 py-2 text-xs font-semibold rounded-xl bg-gradient-to-r from-brand-600 to-indigo-600 hover:from-brand-500 hover:to-indigo-500 text-white shadow-md shadow-brand-500/20 transition flex items-center gap-2">
              <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" /></svg>
              <span>Publish Approved to YouTube</span>
            </button>
          </div>
        </div>
      </section>

    </main>
  </div>

  <!-- ================================================================= -->
  <!-- SLIDE-OVER TASK INSPECTOR DRAWER (Figma Detail Panel)            -->
  <!-- ================================================================= -->
  <div id="drawer-container" class="fixed inset-0 z-50 pointer-events-none">
    <!-- Backdrop -->
    <button type="button" id="drawer-backdrop" aria-label="Close task inspector" onclick="closeDrawer()" class="drawer-overlay fixed inset-0 bg-black/60 backdrop-blur-xs opacity-0 pointer-events-none transition-opacity duration-200 w-full h-full border-0 cursor-default"></button>
    
    <!-- Drawer Panel -->
    <aside id="drawer-panel" class="drawer-panel fixed inset-y-0 right-0 max-w-xl w-full bg-card border-l border-border shadow-2xl flex flex-col pointer-events-auto transform translate-x-full">
      <!-- Drawer Header -->
      <div class="h-16 border-b border-border px-6 flex items-center justify-between bg-card-subtle">
        <div class="flex items-center gap-3">
          <div class="w-8 h-8 rounded-lg bg-brand-500/10 border border-brand-500/20 flex items-center justify-center text-brand-400">
            <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" /></svg>
          </div>
          <div>
            <h2 id="drawer-filename" class="text-sm font-bold text-white font-mono truncate max-w-xs sm:max-w-md">video.mp4</h2>
            <div class="text-[11px] text-zinc-400 flex items-center gap-2">
              <span id="drawer-status-badge" class="px-2 py-0.5 rounded text-[10px] font-semibold uppercase">STATUS</span>
              <span>•</span>
              <span id="drawer-id-label" class="font-mono text-zinc-400">ID: v_xxx</span>
            </div>
          </div>
        </div>
        <button onclick="closeDrawer()" class="p-2 rounded-lg text-zinc-400 hover:text-white hover:bg-zinc-800 transition">
          <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
        </button>
      </div>

      <!-- Drawer Body -->
      <div class="flex-1 overflow-y-auto p-6 space-y-6 text-xs">

        <!-- AI Match Hero Card -->
        <div class="bg-gradient-to-br from-indigo-950/40 via-card-subtle to-canvas border border-indigo-900/40 rounded-2xl p-4 space-y-3">
          <div class="flex items-center justify-between">
            <span class="text-[10px] font-bold tracking-wider uppercase text-indigo-400 flex items-center gap-1.5">
              <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3" /></svg>
              Identified Track & Artist
            </span>
            <span id="drawer-confidence-pill" class="px-2 py-0.5 rounded-full font-mono font-bold text-emerald-400 bg-emerald-500/10 border border-emerald-500/20 text-[10px]">95% Match</span>
          </div>

          <div>
            <h3 id="drawer-artist-name" class="text-lg font-bold text-white tracking-tight">Artist Name</h3>
            <div id="drawer-song-title" class="text-base text-brand-400 font-semibold mt-0.5">Song Title</div>
          </div>

          <!-- Confidence Meter Bar -->
          <div class="space-y-1 pt-1">
            <div class="flex justify-between text-[10px] text-zinc-400 font-mono">
              <span>Match Confidence</span>
              <span id="drawer-confidence-text" class="text-zinc-300 font-bold">95.0%</span>
            </div>
            <div class="w-full bg-zinc-800 rounded-full h-1.5 overflow-hidden">
              <div id="drawer-confidence-bar" class="bg-emerald-500 h-1.5 rounded-full transition-all duration-300" style="width: 95%"></div>
            </div>
          </div>
        </div>

        <!-- Venue & Concert Metadata -->
        <div class="bg-card-subtle border border-border rounded-2xl p-4 space-y-3">
          <div class="text-[10px] font-bold tracking-wider uppercase text-zinc-400 flex items-center gap-1.5">
            <svg class="w-3.5 h-3.5 text-zinc-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z" /><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 11a3 3 0 11-6 0 3 3 0 016 0z" /></svg>
            Live Show & Venue Details
          </div>

          <div class="grid grid-cols-2 gap-3">
            <div class="bg-canvas p-2.5 rounded-xl border border-border">
              <span class="text-zinc-400 block text-[10px]">Venue</span>
              <span id="drawer-venue-name" class="font-semibold text-zinc-200 mt-0.5 block truncate">Red Rocks Amphitheatre</span>
              <span id="drawer-venue-city" class="text-zinc-400 block text-[10px] truncate">Morrison, CO</span>
            </div>
            <div class="bg-canvas p-2.5 rounded-xl border border-border">
              <span class="text-zinc-400 block text-[10px]">Event Date</span>
              <span id="drawer-concert-date" class="font-semibold text-zinc-200 mt-0.5 block">2024-08-15</span>
              <span id="drawer-tour-name" class="text-zinc-400 block text-[10px] truncate">World Tour</span>
            </div>
          </div>
        </div>

        <!-- Video Technical Specs -->
        <div class="bg-card-subtle border border-border rounded-2xl p-4 space-y-3">
          <div class="text-[10px] font-bold tracking-wider uppercase text-zinc-400">Media Specifications</div>
          <div class="grid grid-cols-3 gap-2 font-mono text-[11px]">
            <div class="bg-canvas p-2.5 rounded-xl border border-border">
              <span class="text-zinc-400 block text-[10px] font-sans">Duration</span>
              <span id="drawer-duration" class="font-semibold text-zinc-200">45s</span>
            </div>
            <div class="bg-canvas p-2.5 rounded-xl border border-border">
              <span class="text-zinc-400 block text-[10px] font-sans">Resolution</span>
              <span id="drawer-resolution" class="font-semibold text-zinc-200">1920x1080</span>
            </div>
            <div class="bg-canvas p-2.5 rounded-xl border border-border">
              <span class="text-zinc-400 block text-[10px] font-sans">Recorded UTC</span>
              <span id="drawer-recorded-time" class="font-semibold text-zinc-200 truncate block">2024-08-15</span>
            </div>
          </div>
        </div>

        <!-- YouTube Publishing Settings (Editable) -->
        <div class="bg-card-subtle border border-border rounded-2xl p-4 space-y-3">
          <div class="flex items-center justify-between">
            <span class="text-[10px] font-bold tracking-wider uppercase text-zinc-400 flex items-center gap-1.5">
              <svg class="w-3.5 h-3.5 text-red-500" fill="currentColor" viewBox="0 0 24 24"><path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/></svg>
              YouTube Publication Configuration
            </span>
            <span class="text-[10px] text-zinc-400">Custom Title & Privacy</span>
          </div>

          <div class="space-y-3">
            <div>
              <label class="block text-zinc-400 mb-1 text-[11px] font-medium">Video Title Override</label>
              <input type="text" id="drawer-title-input" class="w-full bg-canvas border border-border rounded-xl px-3 py-2 text-zinc-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:border-brand-500 text-xs">
            </div>

            <div>
              <label class="block text-zinc-400 mb-1 text-[11px] font-medium">Privacy Status</label>
              <select id="drawer-privacy-input" class="w-full bg-canvas border border-border rounded-xl px-3 py-2 text-zinc-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:border-brand-500 text-xs">
                <option value="PUBLIC">PUBLIC (Everyone can see and search)</option>
                <option value="UNLISTED" selected>UNLISTED (Anyone with the link can view)</option>
                <option value="PRIVATE">PRIVATE (Only authenticated creator can view)</option>
              </select>
            </div>
          </div>
        </div>

      </div>

      <!-- Drawer Footer Actions -->
      <div class="p-4 border-t border-border bg-card-subtle flex items-center justify-end gap-2.5">
        <button onclick="handleDrawerReject()" id="drawer-btn-reject" class="px-4 py-2 text-xs font-semibold rounded-xl bg-card border border-border hover:bg-rose-950/40 hover:text-rose-300 hover:border-rose-800 text-zinc-300 transition">
          Reject Candidate
        </button>
        <button onclick="handleDrawerApprove()" id="drawer-btn-approve" class="px-5 py-2 text-xs font-semibold rounded-xl bg-gradient-to-r from-brand-600 to-indigo-600 hover:from-brand-500 hover:to-indigo-500 text-white shadow-md shadow-brand-500/20 transition">
          Approve & Queue Upload
        </button>
      </div>
    </aside>
  </div>

  <!-- ================================================================= -->
  <!-- MODAL: SCAN & DISCOVERY                                           -->
  <!-- ================================================================= -->
  <div id="scan-modal" class="hidden fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4">
    <div class="bg-card border border-border rounded-2xl p-6 max-w-lg w-full space-y-4 shadow-2xl">
      <div class="flex items-center justify-between border-b border-border pb-3">
        <div class="flex items-center gap-3">
          <div class="w-8 h-8 rounded-lg bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center text-indigo-400">
            <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0zM10 7v3m0 0v3m0-3h3m-3 0H7" /></svg>
          </div>
          <div>
            <h3 class="font-bold text-sm text-zinc-100">Media Discovery Scan</h3>
            <p class="text-[11px] text-zinc-400">Search and ingest concert footage into the pipeline</p>
          </div>
        </div>
        <button onclick="closeScanModal()" class="text-zinc-400 hover:text-white p-1">
          <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
        </button>
      </div>

      <!-- Mode Selector Tabs -->
      <div class="flex bg-canvas p-1 rounded-xl border border-border text-xs">
        <button type="button" id="scan-tab-btn-drive" onclick="setScanMode('drive')" class="flex-1 py-1.5 rounded-lg bg-zinc-800 text-zinc-200 font-semibold transition">Google Drive</button>
        <button type="button" id="scan-tab-btn-photos" onclick="setScanMode('photos')" class="flex-1 py-1.5 rounded-lg text-zinc-400 hover:text-zinc-200 font-semibold transition">Google Photos</button>
        <button type="button" id="scan-tab-btn-local" onclick="setScanMode('local')" class="flex-1 py-1.5 rounded-lg text-zinc-400 hover:text-zinc-200 font-semibold transition">Local Directory</button>
      </div>

      <!-- Feedback / Error Notice -->
      <div id="scan-feedback-box" class="hidden text-xs"></div>

      <!-- Mode: Google Drive -->
      <div id="scan-mode-drive" class="space-y-3 text-xs">
        <div class="bg-indigo-950/30 border border-indigo-900/40 rounded-xl p-3 text-[11px] text-indigo-200">
          <span>⚡ Google Drive API supports full video search & date filtering without deprecation blocks.</span>
        </div>
        <div>
          <label class="block text-zinc-400 mb-1">Start Date</label>
          <input type="date" id="scan-drive-start-date" class="w-full bg-canvas border border-border rounded-xl px-3 py-2 text-zinc-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 font-mono">
        </div>
        <div>
          <label class="block text-zinc-400 mb-1">End Date</label>
          <input type="date" id="scan-drive-end-date" class="w-full bg-canvas border border-border rounded-xl px-3 py-2 text-zinc-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 font-mono">
        </div>
        <div>
          <label class="block text-zinc-400 mb-1">Drive Folder ID (Optional)</label>
          <input type="text" id="scan-drive-folder" placeholder="Leave blank to scan entire Drive" class="w-full bg-canvas border border-border rounded-xl px-3 py-2 text-zinc-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 font-mono">
        </div>
        <div class="flex items-center justify-between pt-1 text-zinc-400">
          <div class="flex items-center gap-1.5">
            <span class="text-[11px]">Presets:</span>
            <button type="button" onclick="setQuickDriveDays(7)" class="px-2 py-0.5 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-[10px]">7d</button>
            <button type="button" onclick="setQuickDriveDays(30)" class="px-2 py-0.5 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-[10px]">30d</button>
            <button type="button" onclick="setQuickDriveDays(90)" class="px-2 py-0.5 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-[10px]">90d</button>
            <button type="button" onclick="setQuickDriveDays(365)" class="px-2 py-0.5 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-[10px]">1y</button>
          </div>
          <label class="flex items-center gap-1.5 cursor-pointer">
            <input type="checkbox" id="scan-drive-dry-run" class="rounded bg-canvas border-border text-brand-500">
            <span class="text-[11px]">Dry Run</span>
          </label>
        </div>
      </div>

      <!-- Mode: Google Photos -->
      <div id="scan-mode-photos" class="hidden space-y-3 text-xs">
        <div>
          <label class="block text-zinc-400 mb-1">Start Date</label>
          <input type="date" id="scan-start-date" class="w-full bg-canvas border border-border rounded-xl px-3 py-2 text-zinc-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 font-mono">
        </div>
        <div>
          <label class="block text-zinc-400 mb-1">End Date</label>
          <input type="date" id="scan-end-date" class="w-full bg-canvas border border-border rounded-xl px-3 py-2 text-zinc-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 font-mono">
        </div>
        <div class="flex items-center justify-between pt-1 text-zinc-400">
          <div class="flex items-center gap-1.5">
            <span class="text-[11px]">Presets:</span>
            <button type="button" onclick="setQuickDays(7)" class="px-2 py-0.5 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-[10px]">7d</button>
            <button type="button" onclick="setQuickDays(30)" class="px-2 py-0.5 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-[10px]">30d</button>
            <button type="button" onclick="setQuickDays(90)" class="px-2 py-0.5 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-[10px]">90d</button>
            <button type="button" onclick="setQuickDays(365)" class="px-2 py-0.5 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-[10px]">1y</button>
          </div>
          <label class="flex items-center gap-1.5 cursor-pointer">
            <input type="checkbox" id="scan-dry-run" class="rounded bg-canvas border-border text-brand-500">
            <span class="text-[11px]">Dry Run</span>
          </label>
        </div>

        <div class="pt-2 border-t border-border flex items-center justify-between">
          <span class="text-[11px] text-zinc-400">Or select directly in Photos Picker:</span>
          <button type="button" onclick="launchPickerSession()" id="btn-picker-launch" class="px-3 py-1.5 text-xs rounded-xl bg-card hover:bg-zinc-800 text-brand-400 border border-brand-500/30 transition">
            Launch Photos Picker ↗
          </button>
        </div>
      </div>

      <!-- Mode: Local Directory -->
      <div id="scan-mode-local" class="hidden space-y-3 text-xs">
        <div>
          <label class="block text-zinc-400 mb-1">Local Directory Path</label>
          <input type="text" id="scan-local-dir" placeholder="/home/workspace/concert_videos" class="w-full bg-canvas border border-border rounded-xl px-3 py-2 text-zinc-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 font-mono">
          <p class="text-[11px] text-zinc-400 mt-1">Scan any folder containing concert video files (.mp4, .mov, .m4v).</p>
        </div>
      </div>

      <div class="flex items-center justify-end gap-2.5 pt-3 border-t border-border">
        <button onclick="closeScanModal()" class="px-4 py-2 text-xs rounded-xl bg-card hover:bg-zinc-800 text-zinc-300 transition">Cancel</button>
        <button onclick="executeScan()" id="btn-run-scan" class="px-5 py-2 text-xs font-semibold rounded-xl bg-gradient-to-r from-brand-600 to-indigo-600 hover:from-brand-500 hover:to-indigo-500 text-white shadow-md shadow-brand-500/20 transition">Start Discovery</button>
      </div>
    </div>
  </div>

  <script>
    let activeTab = 'board';
    let activeStatusFilter = 'ALL';
    let searchQuery = '';
    let videosData = [];
    let statusData = {};
    let activeSelectedVideo = null;

    function formatDuration(sec) {
      if (sec === null || sec === undefined) return 'Unprobed';
      if (sec === 0) return '0s (Still image)';
      const s = Math.round(sec);
      const m = Math.floor(s / 60);
      const remS = s % 60;
      if (m > 0) return `${m}m ${remS}s`;
      return `${s}s`;
    }

    async function loadData() {
      const refreshIcon = document.getElementById('refresh-icon');
      if (refreshIcon) refreshIcon.classList.add('animate-spin');

      try {
        const [resStatus, resVideos] = await Promise.all([
          fetch('/api/status').then(r => r.json()),
          fetch('/api/videos').then(r => r.json())
        ]);

        statusData = resStatus;
        videosData = resVideos.videos || [];

        // Update Statistics
        const total = statusData.total_videos || 0;
        const pending = statusData.status_counts.PENDING_REVIEW || 0;
        const approved = statusData.status_counts.APPROVED || 0;
        const published = statusData.status_counts.PUBLISHED || 0;

        document.getElementById('stat-total').innerText = total;
        document.getElementById('stat-pending').innerText = pending;
        document.getElementById('stat-approved').innerText = approved;
        document.getElementById('stat-published').innerText = published;

        // Nav counts
        document.getElementById('nav-count-board').innerText = total;
        document.getElementById('nav-count-review').innerText = pending;
        document.getElementById('nav-count-catalog').innerText = total;
        document.getElementById('tab-count-review').innerText = pending;
        document.getElementById('tab-count-catalog').innerText = total;
        document.getElementById('top-approved-count').innerText = approved;

        // Quota meters
        const used = statusData.uploads_today || 0;
        const maxQ = statusData.max_daily_uploads || 5;
        const pct = Math.min(100, Math.round((used / maxQ) * 100));
        document.getElementById('sidebar-quota-text').innerText = `${used} / ${maxQ}`;
        document.getElementById('sidebar-quota-bar').style.width = `${pct}%`;
        document.getElementById('text-quota').innerText = `Daily Quota: ${used} / ${maxQ} uploads used`;

        // Services health indicators
        updateDot('dot-drive', 'text-drive', statusData.services.google_drive?.authenticated);
        updateDot('dot-photos', 'text-photos', statusData.services.google_photos?.authenticated);
        updateDot('dot-yt', 'text-yt', statusData.services.youtube?.authenticated);
        updateDot('dot-setlist', 'text-setlist', statusData.services.setlist_fm?.has_key);
        updateDot('dot-gemini', 'text-gemini', statusData.services.gemini_audio?.has_key);

        renderCurrentView();
      } catch (err) {
        console.error('Failed to load status', err);
      } finally {
        if (refreshIcon) refreshIcon.classList.remove('animate-spin');
      }
    }

    function updateDot(dotId, textId, isActive) {
      const dot = document.getElementById(dotId);
      const text = document.getElementById(textId);
      if (!dot || !text) return;

      if (isActive) {
        dot.className = 'w-2 h-2 rounded-full bg-emerald-500 shadow-sm shadow-emerald-500/50';
        text.innerText = 'Connected';
        text.className = 'text-emerald-400 font-semibold text-xs';
      } else {
        dot.className = 'w-2 h-2 rounded-full bg-zinc-600';
        text.innerText = 'Not Configured';
        text.className = 'text-zinc-500 text-xs';
      }
    }

    function setTab(tab) {
      activeTab = tab;
      const views = ['board', 'review', 'catalog', 'overview'];
      const viewNames = {
        'board': 'Kanban Board',
        'review': 'Review Queue',
        'catalog': 'Video Catalog',
        'overview': 'Overview & KPIs'
      };

      views.forEach(v => {
        const sec = document.getElementById(`view-${v}`);
        const navBtn = document.getElementById(`nav-btn-${v}`);
        const tabBtn = document.getElementById(`tab-btn-${v}`);

        if (v === tab) {
          if (sec) sec.classList.remove('hidden');
          if (navBtn) navBtn.className = 'w-full flex items-center justify-between px-3 py-2 rounded-lg text-xs font-semibold bg-brand-500/10 text-brand-300 border border-brand-500/20';
          if (tabBtn) tabBtn.className = 'px-3 py-1 rounded-lg font-semibold bg-brand-600 text-white shadow-sm flex items-center gap-1.5';
        } else {
          if (sec) sec.classList.add('hidden');
          if (navBtn) navBtn.className = 'w-full flex items-center justify-between px-3 py-2 rounded-lg text-xs font-medium text-zinc-300 hover:text-white hover:bg-zinc-800/50 transition';
          if (tabBtn) tabBtn.className = 'px-3 py-1 rounded-lg font-medium text-zinc-400 hover:text-zinc-200 transition flex items-center gap-1.5';
        }
      });

      document.getElementById('breadcrumb-active-view').innerText = viewNames[tab] || tab;
      document.getElementById('page-title').innerText = viewNames[tab] || 'Task Management Pipeline';

      renderCurrentView();
    }

    function setStatusFilter(filter) {
      activeStatusFilter = filter;
      const filters = ['ALL', 'PENDING_REVIEW', 'APPROVED', 'PUBLISHED', 'HIGH_CONF'];
      filters.forEach(f => {
        const btn = document.getElementById(`filter-btn-${f}`);
        if (!btn) return;
        if (f === filter) {
          btn.className = 'px-2.5 py-1 rounded-lg border text-[11px] font-semibold bg-zinc-800 text-white border-zinc-600 shadow-xs';
        } else {
          btn.className = 'px-2.5 py-1 rounded-lg border text-[11px] font-medium bg-card border-border text-zinc-400 hover:text-zinc-200 transition';
        }
      });
      renderCurrentView();
    }

    function onSearchInput(val) {
      searchQuery = (val || '').toLowerCase().trim();
      renderCurrentView();
    }

    function getFilteredVideos() {
      return videosData.filter(v => {
        const c = v.concert || {};
        const ident = v.identification || {};
        const artist = (ident.detected_artist || c.headliner_artist || '').toLowerCase();
        const song = (ident.detected_song_title || '').toLowerCase();
        const venue = (v.venue ? `${v.venue.name} ${v.venue.city}` : '').toLowerCase();
        const fname = (v.filename || '').toLowerCase();
        const conf = ident.match_confidence || 0;

        // Search text filter
        if (searchQuery) {
          const matched = artist.includes(searchQuery) ||
                          song.includes(searchQuery) ||
                          venue.includes(searchQuery) ||
                          fname.includes(searchQuery) ||
                          v.id.includes(searchQuery);
          if (!matched) return false;
        }

        // Status pill filter
        if (activeStatusFilter === 'ALL') return true;
        if (activeStatusFilter === 'HIGH_CONF') return conf >= 0.90;
        return v.processing_status === activeStatusFilter;
      });
    }

    function renderCurrentView() {
      if (activeTab === 'board') renderBoard();
      else if (activeTab === 'review') renderReviewQueue();
      else if (activeTab === 'catalog') renderCatalog();
    }

    function renderBoard() {
      const filtered = getFilteredVideos();

      // Group into 4 columns
      const cols = {
        discovered: [],
        review: [],
        approved: [],
        published: []
      };

      filtered.forEach(v => {
        if (v.processing_status === 'PENDING_REVIEW') cols.review.push(v);
        else if (v.processing_status === 'APPROVED') cols.approved.push(v);
        else if (v.processing_status === 'PUBLISHED') cols.published.push(v);
        else cols.discovered.push(v);
      });

      document.getElementById('board-count-discovered').innerText = cols.discovered.length;
      document.getElementById('board-count-review').innerText = cols.review.length;
      document.getElementById('board-count-approved').innerText = cols.approved.length;
      document.getElementById('board-count-published').innerText = cols.published.length;

      document.getElementById('board-col-discovered').innerHTML = renderColumnCards(cols.discovered, 'discovered');
      document.getElementById('board-col-review').innerHTML = renderColumnCards(cols.review, 'review');
      document.getElementById('board-col-approved').innerHTML = renderColumnCards(cols.approved, 'approved');
      document.getElementById('board-col-published').innerHTML = renderColumnCards(cols.published, 'published');
    }

    function renderColumnCards(videos, colKey) {
      if (videos.length === 0) {
        return `<div class="p-6 text-center text-zinc-400 text-xs border border-dashed border-border/70 rounded-xl">No tasks in this lane</div>`;
      }

      return videos.map(v => {
        const c = v.concert || {};
        const ident = v.identification || {};
        const artist = ident.detected_artist || c.headliner_artist || 'Unknown Artist';
        const song = ident.detected_song_title || 'Unidentified Clip';
        const venue = v.venue ? `${v.venue.name}, ${v.venue.city}` : 'No venue detected';
        const conf = ident.match_confidence ? Math.round(ident.match_confidence * 100) : 0;
        const durStr = formatDuration(v.duration_seconds);

        let confBadge = `<span class="text-[10px] font-mono px-2 py-0.5 rounded-md bg-zinc-800 text-zinc-400">Score: ${conf}%</span>`;
        if (conf >= 80) {
          confBadge = `<span class="text-[10px] font-mono px-2 py-0.5 rounded-md bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-semibold">${conf}% Match</span>`;
        } else if (conf >= 50) {
          confBadge = `<span class="text-[10px] font-mono px-2 py-0.5 rounded-md bg-amber-500/10 text-amber-400 border border-amber-500/20 font-semibold">${conf}% Match</span>`;
        }

        return `
          <div class="bg-card border border-border rounded-xl p-3.5 hover:border-brand-500/50 hover:shadow-lg hover:shadow-brand-500/5 transition space-y-2.5 group">
            <button type="button" onclick="openDrawer('${v.id}')" class="w-full text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 rounded-lg space-y-2.5 block">
              <div class="flex items-center justify-between text-[11px]">
                <span class="font-mono text-zinc-400 truncate max-w-[130px]">${v.filename}</span>
                <span class="font-mono px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-300 text-[10px]">${durStr} • ${v.height ? v.height + 'p' : 'HD'}</span>
              </div>

              <div>
                <div class="font-bold text-xs text-zinc-100 group-hover:text-brand-300 transition line-clamp-1">${artist}</div>
                <div class="text-xs text-brand-400 font-medium line-clamp-1">${song}</div>
              </div>

              <div class="text-[11px] text-zinc-400 flex items-center gap-1.5 truncate">
                <svg class="w-3.5 h-3.5 text-zinc-400 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z" /></svg>
                <span class="truncate">${venue}</span>
              </div>
            </button>

            <div class="pt-2 border-t border-border/50 flex items-center justify-between">
              ${confBadge}
              ${colKey === 'review' ? `
                <button type="button" onclick="quickApprove('${v.id}')" class="px-2.5 py-1 text-[11px] font-semibold rounded-lg bg-brand-600 hover:bg-brand-500 text-white transition">
                  Approve ✓
                </button>
              ` : `
                <button type="button" onclick="openDrawer('${v.id}')" class="text-[10px] text-zinc-400 hover:text-white transition">Inspect →</button>
              `}
            </div>
          </div>
        `;
      }).join('');
    }

    function renderReviewQueue() {
      const container = document.getElementById('review-list');
      const pending = getFilteredVideos().filter(v => v.processing_status === 'PENDING_REVIEW');

      if (pending.length === 0) {
        container.innerHTML = `
          <div class="bg-card border border-border rounded-2xl p-12 text-center max-w-lg mx-auto space-y-3">
            <div class="w-12 h-12 rounded-2xl bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 flex items-center justify-center mx-auto">
              <svg class="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" /></svg>
            </div>
            <h3 class="font-bold text-base text-zinc-100">Review Queue is Empty!</h3>
            <p class="text-xs text-zinc-400 leading-relaxed">All candidate videos have been triaged. Ingest new footage or run the AI matcher to identify additional tracks.</p>
            <div class="pt-2 flex justify-center gap-2">
              <button onclick="openScanModal()" class="px-3.5 py-1.5 text-xs font-semibold rounded-xl bg-card border border-border hover:bg-zinc-800 text-zinc-200">Scan Media</button>
              <button onclick="triggerAction('process')" class="px-3.5 py-1.5 text-xs font-semibold rounded-xl bg-brand-600 hover:bg-brand-500 text-white">Run AI Processing</button>
            </div>
          </div>
        `;
        return;
      }

      container.innerHTML = pending.map(v => {
        const c = v.concert || {};
        const ident = v.identification || {};
        const conf = ident.match_confidence ? Math.round(ident.match_confidence * 100) : 0;
        const artist = ident.detected_artist || c.headliner_artist || 'Unknown Artist';
        const song = ident.detected_song_title || 'Live Performance';
        const venue = v.venue ? `${v.venue.name}, ${v.venue.city}` : 'Unknown Venue';
        const date = c.event_date || (v.recorded_at_utc ? v.recorded_at_utc.substring(0, 10) : 'Unknown Date');

        return `
          <div class="bg-card border border-border rounded-2xl p-5 hover:border-zinc-700 transition space-y-4 shadow-sm">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-border pb-3.5">
              <button type="button" class="text-left cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 rounded-lg p-1 -m-1" onclick="openDrawer('${v.id}')">
                <span class="text-xs font-mono text-zinc-400">${v.filename}</span>
                <h3 class="text-base font-bold text-zinc-100 flex items-center gap-2 mt-0.5">
                  <span class="hover:text-brand-300 transition">${artist}</span>
                  <span class="text-zinc-400 font-normal">—</span>
                  <span class="text-brand-400 font-semibold">${song}</span>
                </h3>
              </button>
              <div class="flex items-center gap-2">
                <span class="text-xs px-2.5 py-1 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-mono font-semibold">
                  ${conf}% Match
                </span>
                <span class="text-xs px-2.5 py-1 rounded-lg bg-amber-500/10 text-amber-400 border border-amber-500/20 font-semibold">
                  Pending Review
                </span>
              </div>
            </div>

            <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
              <div class="bg-canvas p-3 rounded-xl border border-border">
                <span class="text-zinc-400 block text-[10px] uppercase font-semibold">Venue & Location</span>
                <span class="font-medium text-zinc-200 mt-0.5 block truncate">${venue}</span>
              </div>
              <div class="bg-canvas p-3 rounded-xl border border-border">
                <span class="text-zinc-400 block text-[10px] uppercase font-semibold">Show Date & Tour</span>
                <span class="font-medium text-zinc-200 mt-0.5 block truncate">${date} ${c.tour_name ? '(' + c.tour_name + ')' : ''}</span>
              </div>
              <div class="bg-canvas p-3 rounded-xl border border-border font-mono">
                <span class="text-zinc-400 block text-[10px] font-sans uppercase font-semibold">Video Specs</span>
                <span class="font-medium text-zinc-200 mt-0.5 block">${formatDuration(v.duration_seconds)} • ${v.width && v.height ? `${v.width}x${v.height}` : 'Resolution pending'}</span>
              </div>
            </div>

            <div class="flex items-center justify-between pt-2">
              <button onclick="openDrawer('${v.id}')" class="text-xs text-zinc-400 hover:text-brand-300 flex items-center gap-1">
                <span>View Full Metadata & Setlist</span>
                <span>→</span>
              </button>
              <div class="flex items-center gap-2">
                <button onclick="rejectVideo('${v.id}')" class="px-3.5 py-1.5 text-xs font-semibold rounded-xl bg-card-subtle hover:bg-rose-950/40 hover:text-rose-300 hover:border-rose-800 border border-border transition">
                  Reject
                </button>
                <button onclick="quickApprove('${v.id}')" class="px-4 py-1.5 text-xs font-semibold rounded-xl bg-gradient-to-r from-brand-600 to-indigo-600 hover:from-brand-500 hover:to-indigo-500 text-white shadow-md shadow-brand-500/20 transition">
                  Approve & Queue Upload
                </button>
              </div>
            </div>
          </div>
        `;
      }).join('');
    }

    function renderCatalog() {
      const tbody = document.getElementById('catalog-table-body');
      const filtered = getFilteredVideos();
      document.getElementById('catalog-count-label').innerText = `${filtered.length} of ${videosData.length} videos`;

      if (filtered.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center py-12 text-zinc-400 font-sans">No matching videos in catalog.</td></tr>';
        return;
      }

      tbody.innerHTML = filtered.map(v => {
        const c = v.concert || {};
        const ident = v.identification || {};
        const artist = ident.detected_artist || c.headliner_artist || '—';
        const song = ident.detected_song_title || '—';
        const venue = v.venue ? `${v.venue.name}` : '—';
        const conf = ident.match_confidence ? Math.round(ident.match_confidence * 100) + '%' : '—';

        let badgeClass = 'bg-zinc-800 text-zinc-400 border-zinc-700';
        if (v.processing_status === 'APPROVED') badgeClass = 'bg-indigo-500/10 text-indigo-400 border-indigo-500/30';
        if (v.processing_status === 'PUBLISHED') badgeClass = 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30';
        if (v.processing_status === 'REJECTED') badgeClass = 'bg-rose-500/10 text-rose-400 border-rose-500/30';
        if (v.processing_status === 'PENDING_REVIEW') badgeClass = 'bg-amber-500/10 text-amber-400 border-amber-500/30';

        return `
          <tr onclick="openDrawer('${v.id}')" class="hover:bg-card-hover/60 transition cursor-pointer">
            <td class="px-5 py-3.5 font-mono text-zinc-300">
              <div class="truncate max-w-[160px]">${v.filename}</div>
              <div class="text-[10px] text-zinc-400 font-sans">${v.recorded_at_utc ? v.recorded_at_utc.substring(0, 10) : '—'} • ${formatDuration(v.duration_seconds)}</div>
            </td>
            <td class="px-5 py-3.5 font-medium text-zinc-200">
              <div class="font-semibold text-white truncate max-w-[200px]">${artist}</div>
              <div class="text-[11px] text-brand-400 font-normal truncate max-w-[200px]">${song}</div>
            </td>
            <td class="px-5 py-3.5 text-zinc-300 truncate max-w-[160px]">${venue}</td>
            <td class="px-5 py-3.5 font-mono text-zinc-300 font-semibold">${conf}</td>
            <td class="px-5 py-3.5">
              <span class="px-2.5 py-0.5 rounded-full text-[10px] font-semibold border ${badgeClass}">
                ${v.processing_status}
              </span>
            </td>
            <td class="px-5 py-3.5 text-right font-sans" onclick="event.stopPropagation()">
              <button onclick="openDrawer('${v.id}')" class="text-xs font-semibold text-zinc-400 hover:text-white px-2 py-1 rounded hover:bg-zinc-800 transition">
                Inspect
              </button>
            </td>
          </tr>
        `;
      }).join('');
    }

    // =================================================================
    // SLIDE-OVER DRAWER ACTIONS
    // =================================================================
    function openDrawer(id) {
      const v = videosData.find(item => item.id === id);
      if (!v) return;

      activeSelectedVideo = v;
      const c = v.concert || {};
      const ident = v.identification || {};
      const venue = v.venue || {};

      document.getElementById('drawer-filename').innerText = v.filename || 'Unknown file';
      document.getElementById('drawer-id-label').innerText = `ID: ${v.id.substring(0, 8)}...`;
      
      const badge = document.getElementById('drawer-status-badge');
      badge.innerText = v.processing_status;
      badge.className = `px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${
        v.processing_status === 'APPROVED' ? 'bg-indigo-500/10 text-indigo-400 border border-indigo-500/30' :
        v.processing_status === 'PUBLISHED' ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30' :
        v.processing_status === 'REJECTED' ? 'bg-rose-500/10 text-rose-400 border border-rose-500/30' :
        'bg-amber-500/10 text-amber-400 border border-amber-500/30'
      }`;

      // AI Match
      const artist = ident.detected_artist || c.headliner_artist || 'Unknown Artist';
      const song = ident.detected_song_title || 'Live Performance';
      const conf = ident.match_confidence ? Math.round(ident.match_confidence * 100) : 0;

      document.getElementById('drawer-artist-name').innerText = artist;
      document.getElementById('drawer-song-title').innerText = song;
      document.getElementById('drawer-confidence-pill').innerText = `${conf}% Match`;
      document.getElementById('drawer-confidence-text').innerText = `${conf}%`;
      document.getElementById('drawer-confidence-bar').style.width = `${conf}%`;

      // Venue / Concert
      document.getElementById('drawer-venue-name').innerText = venue.name || 'Unknown Venue';
      document.getElementById('drawer-venue-city').innerText = `${venue.city || ''}, ${venue.country || ''}`;
      document.getElementById('drawer-concert-date').innerText = c.event_date || (v.recorded_at_utc ? v.recorded_at_utc.substring(0, 10) : '—');
      document.getElementById('drawer-tour-name').innerText = c.tour_name || 'Individual Show';

      // Specs
      document.getElementById('drawer-duration').innerText = formatDuration(v.duration_seconds);
      document.getElementById('drawer-resolution').innerText = v.width && v.height ? `${v.width}x${v.height}` : 'Not analyzed';
      document.getElementById('drawer-recorded-time').innerText = v.recorded_at_utc ? v.recorded_at_utc.replace('T', ' ').substring(0, 16) : '—';

      // Pre-fill YouTube form
      const defaultTitle = `${artist} - ${song} (Live at ${venue.name || 'Concert'}, ${c.event_date ? c.event_date.substring(0, 4) : ''})`;
      document.getElementById('drawer-title-input').value = defaultTitle;

      // Open drawer panel
      document.getElementById('drawer-backdrop').classList.remove('opacity-0', 'pointer-events-none');
      document.getElementById('drawer-panel').classList.remove('translate-x-full');
    }

    function closeDrawer() {
      document.getElementById('drawer-backdrop').classList.add('opacity-0', 'pointer-events-none');
      document.getElementById('drawer-panel').classList.add('translate-x-full');
      activeSelectedVideo = null;
    }

    async function handleDrawerApprove() {
      if (!activeSelectedVideo) return;
      const title = document.getElementById('drawer-title-input').value;
      const privacy = document.getElementById('drawer-privacy-input').value;
      await approveVideo(activeSelectedVideo.id, title, privacy);
      closeDrawer();
    }

    async function handleDrawerReject() {
      if (!activeSelectedVideo) return;
      await rejectVideo(activeSelectedVideo.id);
      closeDrawer();
    }

    async function quickApprove(id) {
      await approveVideo(id);
    }

    async function approveVideo(id, title, privacy) {
      try {
        const payload = {};
        if (title) payload.title = title;
        if (privacy) payload.privacy = privacy;

        const res = await fetch(`/api/video/${id}/approve`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!data.success) {
          alert('Failed to approve video: ' + (data.error || 'Server error'));
        }
        await loadData();
      } catch (err) {
        alert('Failed to approve video: ' + err);
      }
    }

    async function rejectVideo(id) {
      try {
        await fetch(`/api/video/${id}/reject`, { method: 'POST' });
        await loadData();
      } catch (err) {
        alert('Failed to reject video: ' + err);
      }
    }

    async function triggerAction(action) {
      const btn = document.getElementById(`btn-${action}-action`) || document.getElementById(`sidebar-btn-${action}`);
      let orig = '';
      if (btn) {
        orig = btn.innerHTML;
        btn.innerHTML = '<span>⏳ Working...</span>';
        btn.disabled = true;
      }

      try {
        const res = await fetch(`/api/${action}`, { method: 'POST' });
        const data = await res.json();
        await loadData();
      } catch (err) {
        alert(`Action ${action} failed: ` + err);
      } finally {
        if (btn) {
          btn.innerHTML = orig;
          btn.disabled = false;
        }
      }
    }

    // =================================================================
    // SCAN & INGESTION MODAL
    // =================================================================
    function openScanModal() {
      document.getElementById('scan-modal').classList.remove('hidden');
    }

    function closeScanModal() {
      document.getElementById('scan-modal').classList.add('hidden');
    }

    let currentScanMode = 'drive';

    function setScanMode(mode) {
      currentScanMode = mode;
      const btnDrive = document.getElementById('scan-tab-btn-drive');
      const btnPhotos = document.getElementById('scan-tab-btn-photos');
      const btnLocal = document.getElementById('scan-tab-btn-local');
      const viewDrive = document.getElementById('scan-mode-drive');
      const viewPhotos = document.getElementById('scan-mode-photos');
      const viewLocal = document.getElementById('scan-mode-local');
      document.getElementById('scan-feedback-box').classList.add('hidden');

      btnDrive.className = 'flex-1 py-1.5 rounded-lg text-zinc-400 hover:text-zinc-200 font-semibold transition';
      btnPhotos.className = 'flex-1 py-1.5 rounded-lg text-zinc-400 hover:text-zinc-200 font-semibold transition';
      btnLocal.className = 'flex-1 py-1.5 rounded-lg text-zinc-400 hover:text-zinc-200 font-semibold transition';
      viewDrive.classList.add('hidden');
      viewPhotos.classList.add('hidden');
      viewLocal.classList.add('hidden');

      if (mode === 'drive') {
        btnDrive.className = 'flex-1 py-1.5 rounded-lg bg-zinc-800 text-zinc-200 font-semibold transition';
        viewDrive.classList.remove('hidden');
      } else if (mode === 'photos') {
        btnPhotos.className = 'flex-1 py-1.5 rounded-lg bg-zinc-800 text-zinc-200 font-semibold transition';
        viewPhotos.classList.remove('hidden');
      } else {
        btnLocal.className = 'flex-1 py-1.5 rounded-lg bg-zinc-800 text-zinc-200 font-semibold transition';
        viewLocal.classList.remove('hidden');
      }
    }

    function setQuickDriveDays(days) {
      const end = new Date();
      const start = new Date();
      start.setDate(end.getDate() - days);
      document.getElementById('scan-drive-end-date').value = end.toISOString().split('T')[0];
      document.getElementById('scan-drive-start-date').value = start.toISOString().split('T')[0];
    }

    function setQuickDays(days) {
      const end = new Date();
      const start = new Date();
      start.setDate(end.getDate() - days);
      document.getElementById('scan-end-date').value = end.toISOString().split('T')[0];
      document.getElementById('scan-start-date').value = start.toISOString().split('T')[0];
    }

    async function executeScan() {
      const btn = document.getElementById('btn-run-scan');
      const orig = btn.innerText;
      btn.innerText = 'Scanning...';
      btn.disabled = true;
      const feedback = document.getElementById('scan-feedback-box');
      feedback.classList.add('hidden');

      try {
        let payload = {};
        if (currentScanMode === 'local') {
          const dirPath = document.getElementById('scan-local-dir').value.trim();
          if (!dirPath) {
            alert('Please specify a directory path to scan.');
            return;
          }
          payload = { directory: dirPath };
        } else if (currentScanMode === 'drive') {
          const startDate = document.getElementById('scan-drive-start-date').value;
          const endDate = document.getElementById('scan-drive-end-date').value;
          const folderId = document.getElementById('scan-drive-folder').value.trim();
          const dryRun = document.getElementById('scan-drive-dry-run').checked;
          payload = {
            provider: 'google_drive',
            start_date: startDate || undefined,
            end_date: endDate || undefined,
            folder_id: folderId || undefined,
            dry_run: dryRun
          };
        } else {
          const startDate = document.getElementById('scan-start-date').value;
          const endDate = document.getElementById('scan-end-date').value;
          const dryRun = document.getElementById('scan-dry-run').checked;
          payload = {
            provider: 'google_photos',
            start_date: startDate || undefined,
            end_date: endDate || undefined,
            dry_run: dryRun
          };
        }

        const res = await fetch('/api/scan', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();

        if (!data.success) {
          feedback.classList.remove('hidden');
          feedback.innerHTML = `
            <div class="bg-rose-950/40 border border-rose-800 text-rose-200 p-3 rounded-xl">
              <div class="font-bold">Scan Notice:</div>
              <div>${data.error || 'Failed to scan videos.'}</div>
            </div>
          `;
          return;
        }

        closeScanModal();
        await loadData();
        alert(`Discovery completed: Ingested ${data.scanned_count || 0} candidate video(s).`);
      } catch (err) {
        alert('Scan failed: ' + err);
      } finally {
        btn.innerText = orig;
        btn.disabled = false;
      }
    }

    async function launchPickerSession() {
      const btn = document.getElementById('btn-picker-launch');
      const orig = btn.innerText;
      btn.innerText = 'Creating Picker...';
      btn.disabled = true;

      try {
        const res = await fetch('/api/picker/create', { method: 'POST' });
        const data = await res.json();
        if (!data.success) {
          alert('Failed to create Photos Picker session: ' + (data.error || 'Unknown error'));
          return;
        }

        const pickerUri = data.session.pickerUri;
        const sessionId = data.session.id;
        window.open(pickerUri, '_blank');

        btn.innerText = 'Waiting for selection...';
        const pollInterval = setInterval(async () => {
          try {
            const pRes = await fetch('/api/picker/poll', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ session_id: sessionId })
            });
            const pData = await pRes.json();
            if (pData.completed) {
              clearInterval(pollInterval);
              btn.innerText = orig;
              btn.disabled = false;
              closeScanModal();
              await loadData();
              alert(`Photos Picker selection complete! Ingested ${pData.ingested_count} video(s).`);
            }
          } catch (e) {
            console.error('Picker poll error:', e);
          }
        }, 4000);
      } catch (err) {
        alert('Failed to initiate Photos Picker: ' + err);
        btn.innerText = orig;
        btn.disabled = false;
      }
    }

    function toggleSidebar() {
      const sidebar = document.getElementById('app-sidebar');
      const backdrop = document.getElementById('sidebar-backdrop');
      const isClosed = sidebar.classList.contains('-translate-x-full');
      if (isClosed) {
        sidebar.classList.remove('-translate-x-full');
        backdrop.classList.remove('hidden');
      } else {
        sidebar.classList.add('-translate-x-full');
        backdrop.classList.add('hidden');
      }
    }

    // Global Keydown Listeners (ESC closes modals & drawers, ⌘K focuses search)
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        closeDrawer();
        closeScanModal();
      }
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        const searchInput = document.getElementById('global-search');
        if (searchInput) searchInput.focus();
      }
    });

    // Initial Data Load & Auto-Refresh
    loadData();
    setTab('board');
    setInterval(loadData, 10000);
  </script>
</body>
</html>
"""


def create_app(
    coordinator: Optional[PipelineCoordinator] = None,
    db: Optional[Database] = None,
    settings: Optional[Settings] = None,
) -> web.Application:
    """Construct and configure the aiohttp web application."""
    app = web.Application()
    app[SETTINGS_KEY] = settings or get_settings()
    app[DB_KEY] = db or get_db()
    app[COORD_KEY] = coordinator or PipelineCoordinator(db=app[DB_KEY], settings=app[SETTINGS_KEY])

    # Register Routes
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_get_status)
    app.router.add_get("/api/videos", handle_list_videos)
    app.router.add_get("/api/video/{id}", handle_get_video)
    app.router.add_post("/api/video/{id}/approve", handle_approve_video)
    app.router.add_post("/api/video/{id}/reject", handle_reject_video)
    app.router.add_post("/api/scan", handle_trigger_scan)
    app.router.add_post("/api/picker/create", handle_picker_create)
    app.router.add_post("/api/picker/poll", handle_picker_poll)
    app.router.add_post("/api/process", handle_trigger_process)
    app.router.add_post("/api/upload", handle_trigger_upload)

    return app


async def handle_index(request: web.Request) -> web.Response:
    """Serve the single-page dashboard HTML."""
    return web.Response(text=HTML_PAGE, content_type="text/html")


async def handle_get_status(request: web.Request) -> web.Response:
    """Return JSON system status, integration health, and quotas."""
    db: Database = request.app[DB_KEY]
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    settings: Settings = request.app[SETTINGS_KEY]

    all_videos = db.list_videos(limit=2000)
    status_counts: Dict[str, int] = {}
    for v in all_videos:
        st = v.processing_status.value
        status_counts[st] = status_counts.get(st, 0) + 1

    uploads_today = db.get_todays_upload_count()

    payload = {
        "total_videos": len(all_videos),
        "status_counts": status_counts,
        "uploads_today": uploads_today,
        "max_daily_uploads": settings.max_daily_uploads,
        "services": {
            "google_drive": {
                "authenticated": bool(coordinator.drive_client.is_authenticated()),
            },
            "google_photos": {
                "authenticated": bool(coordinator.photos_client.is_authenticated()),
            },
            "youtube": {
                "authenticated": bool(coordinator.uploader.auth.is_authenticated()),
            },
            "setlist_fm": {
                "has_key": bool(settings.setlist_fm_api_key),
            },
            "gemini_audio": {
                "has_key": bool(settings.gemini_api_key),
            },
        },
    }
    return web.json_response(payload)


def serialize_model(obj: Any) -> Any:
    """Safely serialize Pydantic or dictionary models to JSON-compatible dict."""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "dict"):
        return obj.dict()
    return obj


async def handle_list_videos(request: web.Request) -> web.Response:
    """Return JSON list of cataloged videos with embedded related records."""
    db: Database = request.app[DB_KEY]
    status_param = request.query.get("status")
    limit = int(request.query.get("limit", 500))

    target_status = VideoStatus(status_param) if status_param else None
    videos = db.list_videos(status=target_status, limit=limit)

    results = []
    for v in videos:
        data = serialize_model(v)
        # Embed related identification, concert, venue, and upload
        ident = db.get_identification_by_video_id(v.id)
        data["identification"] = serialize_model(ident)

        concert = None
        venue = None
        if ident and ident.concert_id:
            concert = db.get_concert(ident.concert_id)
        if concert and concert.venue_id:
            venue = db.get_venue(concert.venue_id)
        elif v.latitude and v.longitude:
            venue = db.find_venue_by_coords(v.latitude, v.longitude)

        data["concert"] = serialize_model(concert)
        data["venue"] = serialize_model(venue)

        upload = db.get_youtube_upload_by_video_id(v.id)
        data["upload"] = serialize_model(upload)

        results.append(data)

    return web.json_response({"videos": results, "count": len(results)})


async def handle_get_video(request: web.Request) -> web.Response:
    """Return JSON for a single video by ID."""
    db: Database = request.app[DB_KEY]
    video_id = request.match_info["id"]
    video = db.get_video(video_id)
    if not video:
        return web.json_response({"error": "Video not found"}, status=404)

    data = serialize_model(video)
    ident = db.get_identification_by_video_id(video.id)
    data["identification"] = serialize_model(ident)

    concert = None
    venue = None
    if ident and ident.concert_id:
        concert = db.get_concert(ident.concert_id)
    if concert and concert.venue_id:
        venue = db.get_venue(concert.venue_id)
    elif video.latitude and video.longitude:
        venue = db.find_venue_by_coords(video.latitude, video.longitude)

    data["concert"] = serialize_model(concert)
    data["venue"] = serialize_model(venue)

    upload = db.get_youtube_upload_by_video_id(video.id)
    data["upload"] = serialize_model(upload)

    return web.json_response(data)


async def handle_approve_video(request: web.Request) -> web.Response:
    """Approve a video for YouTube upload."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]

    body = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}

    title_override = body.get("title")
    privacy_str = body.get("privacy")
    privacy = PrivacyStatus(privacy_str) if privacy_str else None

    try:
        updated = coordinator.approve_video(
            video_id=video_id,
            title_override=title_override,
            privacy_override=privacy,
        )
        return web.json_response({"success": True, "video": serialize_model(updated)})
    except Exception as e:
        logger.exception("Failed to approve video %s: %s", video_id, e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_reject_video(request: web.Request) -> web.Response:
    """Reject a video from upload queue."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]
    try:
        updated = coordinator.reject_video(video_id)
        return web.json_response({"success": True, "video": serialize_model(updated)})
    except Exception as e:
        logger.exception("Failed to reject video %s: %s", video_id, e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_trigger_scan(request: web.Request) -> web.Response:
    """Trigger Google Photos or local directory discovery scan."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    body = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}

    directory = body.get("directory")
    if directory:
        try:
            discovered = coordinator.scan_local_directory(directory)
            return web.json_response({
                "success": True,
                "scanned_count": len(discovered),
                "mode": "local_directory",
            })
        except Exception as e:
            logger.exception("Failed to scan local directory %s: %s", directory, e)
            return web.json_response({
                "success": False,
                "error": str(e),
                "scanned_count": 0,
                "mode": "local_directory",
            })

    start_date = body.get("start_date")
    end_date = body.get("end_date")
    days_val = int(body["days"]) if body.get("days") else None
    limit_val = int(body.get("limit", 50))
    dry_run = bool(body.get("dry_run", False))
    provider = body.get("provider", "google_drive" if body.get("drive") else "google_photos")

    if provider in ("google_drive", "drive") or body.get("drive"):
        folder_id = body.get("folder_id")
        try:
            discovered = coordinator.scan_google_drive(
                start_date=start_date,
                end_date=end_date,
                days=days_val,
                folder_id=folder_id,
                limit=limit_val,
                dry_run=dry_run,
            )
            return web.json_response({
                "success": True,
                "scanned_count": len(discovered),
                "mode": "google_drive",
            })
        except Exception as e:
            logger.warning("Failed to scan Google Drive: %s", e)
            return web.json_response({
                "success": False,
                "error": str(e),
                "scanned_count": 0,
                "mode": "google_drive",
            })

    try:
        discovered = coordinator.scan_google_photos(
            start_date=start_date,
            end_date=end_date,
            days=days_val,
            limit=limit_val,
            dry_run=dry_run,
        )
        return web.json_response({
            "success": True,
            "scanned_count": len(discovered),
            "mode": "google_photos",
        })
    except Exception as e:
        logger.warning("Failed to scan Google Photos: %s", e)
        err_msg = str(e)
        is_scope_error = "403" in err_msg or "insufficient" in err_msg.lower()
        return web.json_response({
            "success": False,
            "error": err_msg,
            "is_scope_error": is_scope_error,
            "scanned_count": 0,
            "mode": "google_photos",
        })


async def handle_picker_create(request: web.Request) -> web.Response:
    """Create a Google Photos Picker session."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    try:
        session = coordinator.photos_client.create_picker_session()
        return web.json_response({"success": True, "session": session})
    except Exception as e:
        logger.warning("Failed to create picker session: %s", e)
        return web.json_response({"success": False, "error": str(e)})


async def handle_picker_poll(request: web.Request) -> web.Response:
    """Poll a Google Photos Picker session and ingest items if complete."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    body = await request.json() if request.can_read_body else {}
    session_id = body.get("session_id")
    dry_run = bool(body.get("dry_run", False))
    if not session_id:
        return web.json_response({"success": False, "error": "Missing session_id"}, status=400)
    try:
        is_done = coordinator.photos_client.poll_picker_session(session_id)
        if not is_done:
            return web.json_response({"success": True, "completed": False})

        items = coordinator.photos_client.get_picker_items(session_id)
        videos = coordinator.ingest_picker_items(items, dry_run=dry_run)
        coordinator.photos_client.delete_picker_session(session_id)
        return web.json_response({
            "success": True,
            "completed": True,
            "ingested_count": len(videos),
        })
    except Exception as e:
        logger.warning("Failed to poll picker session %s: %s", session_id, e)
        return web.json_response({"success": False, "error": str(e)})


async def handle_trigger_process(request: web.Request) -> web.Response:
    """Trigger processing of pending candidate videos."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    try:
        processed = coordinator.process_pending_videos(limit=25)
        return web.json_response({"success": True, "processed_count": len(processed)})
    except Exception as e:
        logger.exception("Failed to process videos: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_trigger_upload(request: web.Request) -> web.Response:
    """Trigger upload of approved videos to YouTube."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    try:
        results = coordinator.upload_approved_videos()
        return web.json_response({
            "success": True,
            "uploaded_count": len([r for r in results if r.success]),
        })
    except Exception as e:
        logger.exception("Failed to upload approved videos: %s", e)
        return web.json_response({"error": str(e)}, status=500)


def run_server(
    host: str = "0.0.0.0",
    port: int = 8765,
    open_browser: bool = False,
) -> None:
    """Start the aiohttp server loop."""
    app = create_app()
    if open_browser:
        webbrowser.open(f"http://{host}:{port}")
    web.run_app(app, host=host, port=port)
