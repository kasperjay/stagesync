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
import mimetypes
from pathlib import Path
from typing import Any, Dict, Optional
import webbrowser

from aiohttp import web

from stagesync.config import Settings, get_settings
from stagesync.database.connection import Database, get_db
from stagesync.database.models import PrivacyStatus, VideoStatus
from stagesync.intelligence.thumbnail_extractor import ThumbnailExtractor
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
        <button onclick="setStatusFilter('REJECTED')" id="filter-btn-REJECTED" class="px-2.5 py-1 rounded-lg border text-[11px] font-medium transition bg-card border-border text-zinc-400 hover:text-zinc-200">Rejected</button>
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

        <!-- Video Still Frame -->
        <div class="relative rounded-2xl overflow-hidden border border-border bg-card-subtle group">
          <img id="drawer-thumbnail" src="" alt="Video Frame" class="w-full h-44 object-cover" />
          <button type="button" onclick="openVenueAssistModal(activeSelectedVideo ? activeSelectedVideo.id : '')" class="absolute inset-0 bg-black/50 backdrop-blur-xs opacity-0 group-hover:opacity-100 flex items-center justify-center gap-2 text-white font-semibold text-xs transition duration-200">
            <svg class="w-4 h-4 text-brand-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" /></svg>
            <span>What venue is this? / Assist Search</span>
          </button>
        </div>

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
            <div class="bg-canvas p-2.5 rounded-xl border border-border flex flex-col justify-between">
              <div>
                <span class="text-zinc-400 block text-[10px]">Venue</span>
                <span id="drawer-venue-name" class="font-semibold text-zinc-200 mt-0.5 block truncate">Red Rocks Amphitheatre</span>
                <span id="drawer-venue-city" class="text-zinc-400 block text-[10px] truncate">Morrison, CO</span>
              </div>
              <button type="button" onclick="openVenueAssistModal(activeSelectedVideo ? activeSelectedVideo.id : '')" class="mt-2 text-[10px] font-semibold text-brand-400 hover:text-brand-300 flex items-center gap-1">
                <svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" /></svg>
                Change / Search Venue →
              </button>
            </div>
            <div class="bg-canvas p-2.5 rounded-xl border border-border">
              <span class="text-zinc-400 block text-[10px]">Event Date</span>
              <span id="drawer-concert-date" class="font-semibold text-zinc-200 mt-0.5 block">2024-08-15</span>
              <span id="drawer-tour-name" class="text-zinc-400 block text-[10px] truncate">World Tour</span>
            </div>
          </div>
        </div>

        <!-- Which band is this? (Concert Lineup Assistant) -->
        <div id="drawer-band-assist-section" class="hidden bg-amber-500/10 border border-amber-500/30 rounded-2xl p-4 space-y-3">
          <div class="flex items-center justify-between">
            <span class="text-[10px] font-bold tracking-wider uppercase text-amber-400 flex items-center gap-1.5">
              <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3" /></svg>
              Which band is this?
            </span>
            <button type="button" onclick="openBandAssistModal(activeSelectedVideo ? activeSelectedVideo.id : '')" class="text-[10px] text-amber-300 font-semibold hover:underline flex items-center gap-1">
              Still Preview →
            </button>
          </div>
          <p class="text-[11px] text-zinc-300 leading-snug">Concert matched, but song/performer is unconfirmed. Select who performed in this clip:</p>
          <div id="drawer-band-chips" class="flex flex-wrap gap-1.5 pt-1">
            <!-- Dynamic band buttons -->
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

  <!-- ================================================================= -->
  <!-- USER-ASSIST VENUE IDENTIFICATION MODAL                            -->
  <!-- ================================================================= -->
  <div id="venue-assist-modal" class="hidden fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
    <div class="bg-card border border-border rounded-2xl max-w-lg w-full p-6 shadow-2xl relative space-y-4 animate-in fade-in zoom-in-95 duration-150">
      
      <!-- Modal Header -->
      <div class="flex items-start justify-between border-b border-border pb-3">
        <div>
          <div class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-mono font-bold bg-brand-500/10 text-brand-400 border border-brand-500/20 mb-1.5">
            <span class="w-1.5 h-1.5 rounded-full bg-brand-400 animate-pulse"></span>
            IDENTIFICATION ASSISTANT
          </div>
          <h3 class="text-lg font-bold text-white tracking-tight">What venue is this?</h3>
          <p id="venue-assist-subtitle" class="text-xs text-zinc-400 mt-0.5">Help StageSync identify the concert venue and correlate the setlist.</p>
        </div>
        <button onclick="closeVenueAssistModal()" class="text-zinc-400 hover:text-white p-1 rounded-lg hover:bg-zinc-800 transition">
          <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
        </button>
      </div>

      <!-- Video Still Preview -->
      <div class="relative rounded-xl overflow-hidden border border-border bg-black/50 aspect-video flex items-center justify-center">
        <img id="venue-assist-thumbnail" src="" alt="Video Frame Preview" class="w-full h-full object-cover" />
        <button type="button" id="venue-assist-refresh" onclick="refreshVideoStill('venue', this); event.stopPropagation();" class="absolute top-2 right-2 inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-black/75 hover:bg-black/90 border border-white/15 text-[10px] font-semibold text-white transition" aria-label="Show a different video frame" title="Show a different video frame">
          <span class="text-sm leading-none">↻</span> Different frame
        </button>
        <div class="absolute bottom-2 left-2 right-2 flex items-center justify-between pointer-events-none">
          <span id="venue-assist-filename-tag" class="px-2 py-1 rounded bg-black/80 backdrop-blur text-[10px] font-mono text-zinc-200 border border-white/10 truncate max-w-[200px]">video.mov</span>
          <span id="venue-assist-meta-tag" class="px-2 py-1 rounded bg-black/80 backdrop-blur text-[10px] font-mono text-zinc-300 border border-white/10">00:00</span>
        </div>
      </div>

      <!-- Live Venue Search Box -->
      <div class="space-y-1.5">
        <label for="venue-search-input" class="text-xs font-semibold text-zinc-300 flex items-center justify-between">
          <span>Search Venue Name</span>
          <span class="text-[10px] text-zinc-400 font-normal">Searches Local DB, Setlist.fm & OpenStreetMap</span>
        </label>
        <div class="relative">
          <input
            type="text"
            id="venue-search-input"
            placeholder="Type venue name (e.g. Come and Take It Live, Red Rocks)..."
            class="w-full bg-canvas border border-border rounded-xl px-3.5 py-2.5 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:border-brand-500 transition pl-9"
            oninput="handleVenueSearchInput(this.value)"
          />
          <svg class="w-4 h-4 text-zinc-400 absolute left-3 top-3 pointer-events-none" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
        </div>
      </div>

      <!-- Candidate Venues List -->
      <div id="venue-search-results" class="max-h-48 overflow-y-auto space-y-1.5 pr-1">
        <div class="text-center py-5 text-zinc-400 text-xs">Type a venue name above to find matches...</div>
      </div>

      <!-- Selected Venue Confirmation Card -->
      <div id="venue-selected-preview" class="hidden p-3 rounded-xl bg-brand-500/10 border border-brand-500/30 text-xs space-y-1">
        <div class="text-[10px] font-bold uppercase tracking-wider text-brand-400">Selected Venue Candidate</div>
        <div id="venue-selected-name" class="font-bold text-white text-sm">Venue Name</div>
        <div id="venue-selected-address" class="text-zinc-300 text-[11px]">Location Details</div>
      </div>

      <!-- Action Buttons -->
      <div class="flex items-center justify-between pt-2 border-t border-border">
        <button type="button" onclick="closeVenueAssistModal()" class="px-4 py-2 text-xs font-semibold rounded-xl bg-card border border-border text-zinc-300 hover:bg-zinc-800 transition">Cancel</button>
        <button type="button" id="btn-confirm-set-venue" onclick="confirmSetVenue()" disabled class="px-5 py-2 text-xs font-semibold rounded-xl bg-gradient-to-r from-brand-600 to-indigo-600 text-white shadow-md shadow-brand-500/20 disabled:opacity-50 disabled:pointer-events-none hover:from-brand-500 hover:to-indigo-500 transition flex items-center gap-2">
          <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" /></svg>
          Confirm & Re-Search Concert
        </button>
      </div>
    </div>
  </div>

  <!-- ================================================================= -->
  <!-- USER-ASSIST BAND SELECTION MODAL ("Which band is this?")         -->
  <!-- ================================================================= -->
  <div id="band-assist-modal" class="hidden fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
    <div class="bg-card border border-border rounded-2xl max-w-lg w-full p-6 shadow-2xl relative space-y-4 animate-in fade-in zoom-in-95 duration-150">
      
      <!-- Modal Header -->
      <div class="flex items-start justify-between border-b border-border pb-3">
        <div>
          <div class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-mono font-bold bg-amber-500/10 text-amber-400 border border-amber-500/20 mb-1.5">
            <span class="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse"></span>
            LINEUP IDENTIFICATION
          </div>
          <h3 class="text-lg font-bold text-white tracking-tight">Which band is this?</h3>
          <p id="band-assist-subtitle" class="text-xs text-zinc-400 mt-0.5">Select from the artists who performed at this show.</p>
        </div>
        <button onclick="closeBandAssistModal(true)" class="text-zinc-400 hover:text-white p-1 rounded-lg hover:bg-zinc-800 transition">
          <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
        </button>
      </div>

      <!-- Video Still Preview -->
      <div class="relative rounded-xl overflow-hidden border border-border bg-black/50 aspect-video flex items-center justify-center">
        <img id="band-assist-thumbnail" src="" alt="Video Frame Preview" class="w-full h-full object-cover" />
        <button type="button" id="band-assist-refresh" onclick="refreshVideoStill('band', this); event.stopPropagation();" class="absolute top-2 right-2 inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-black/75 hover:bg-black/90 border border-white/15 text-[10px] font-semibold text-white transition" aria-label="Show a different video frame" title="Show a different video frame">
          <span class="text-sm leading-none">↻</span> Different frame
        </button>
        <div class="absolute bottom-2 left-2 right-2 flex items-center justify-between pointer-events-none">
          <span id="band-assist-filename-tag" class="px-2 py-1 rounded bg-black/80 backdrop-blur text-[10px] font-mono text-zinc-200 border border-white/10 truncate max-w-[200px]">video.mov</span>
          <span id="band-assist-meta-tag" class="px-2 py-1 rounded bg-black/80 backdrop-blur text-[10px] font-mono text-zinc-300 border border-white/10">00:00</span>
        </div>
      </div>

      <!-- Matched Show Details -->
      <div class="bg-card-subtle border border-border rounded-xl p-3 text-xs space-y-1">
        <div class="text-[10px] uppercase font-bold text-zinc-400 tracking-wider">Matched Concert Show</div>
        <div id="band-assist-show-title" class="font-bold text-zinc-100 text-sm">Attila — About That Life 10 Year Anniversary</div>
        <div id="band-assist-show-venue" class="text-[11px] text-brand-400">Come and Take It Live • Austin, TX • 2023-10-13</div>
      </div>

      <!-- Bands List -->
      <div class="space-y-2">
        <label class="text-xs font-semibold text-zinc-300 flex items-center justify-between">
          <span>Show Lineup</span>
          <span class="text-[10px] text-zinc-400 font-normal">Select the band performing in this footage</span>
        </label>
        <div id="band-options-container" class="max-h-48 overflow-y-auto space-y-1.5 pr-1">
          <!-- Dynamic band cards -->
        </div>
      </div>

      <!-- Custom / Opener Input -->
      <div class="pt-1">
        <div class="flex items-center gap-2">
          <input
            type="text"
            id="band-custom-input"
            placeholder="Or type unlisted opener / performer name..."
            class="flex-1 bg-canvas border border-border rounded-xl px-3 py-2 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 font-sans"
            onkeydown="if(event.key==='Enter') selectCustomBandFromInput()"
          />
          <button
            type="button"
            onclick="selectCustomBandFromInput()"
            class="px-3.5 py-2 text-xs font-semibold rounded-xl bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700 transition"
          >
            Select
          </button>
        </div>
      </div>

      <!-- Selected Performer Confirmation -->
      <div id="band-selected-preview" class="hidden p-3 rounded-xl bg-amber-500/10 border border-amber-500/30 text-xs space-y-1">
        <div class="text-[10px] font-bold uppercase tracking-wider text-amber-400">Selected Performing Artist</div>
        <div id="band-selected-name" class="font-bold text-white text-sm">Band Name</div>
      </div>

      <!-- Action Buttons -->
      <div class="flex items-center justify-between pt-2 border-t border-border">
        <button type="button" onclick="closeBandAssistModal()" class="px-4 py-2 text-xs font-semibold rounded-xl bg-card border border-border text-zinc-300 hover:bg-zinc-800 transition">Cancel</button>
        <button type="button" id="btn-confirm-set-band" onclick="confirmSetBand()" disabled class="px-5 py-2 text-xs font-semibold rounded-xl bg-gradient-to-r from-amber-500 to-brand-600 text-white shadow-md shadow-brand-500/20 disabled:opacity-50 disabled:pointer-events-none hover:from-amber-400 hover:to-brand-500 transition flex items-center gap-2">
          <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" /></svg>
          Confirm Band Selection
        </button>
      </div>
    </div>
  </div>

  <!-- ================================================================= -->
  <!-- USER-ASSIST SONG SELECTION MODAL ("What song is this?")          -->
  <!-- ================================================================= -->
  <div id="song-assist-modal" class="hidden fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
    <div class="bg-card border border-border rounded-2xl max-w-lg w-full p-6 shadow-2xl relative space-y-4 animate-in fade-in zoom-in-95 duration-150">
      <div class="flex items-start justify-between border-b border-border pb-3">
        <div>
          <div class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-mono font-bold bg-rose-500/10 text-rose-400 border border-rose-500/20 mb-1.5">SONG MATCHING</div>
          <h3 class="text-lg font-bold text-white tracking-tight">What song is this?</h3>
          <p id="song-assist-subtitle" class="text-xs text-zinc-400 mt-0.5">Choose the song StageSync could not identify.</p>
        </div>
        <button type="button" onclick="closeSongAssistModal()" class="text-zinc-400 hover:text-white p-1 rounded-lg hover:bg-zinc-800 transition" aria-label="Close song selection">
          <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
        </button>
      </div>
      <div class="bg-card-subtle border border-border rounded-xl p-3 text-xs space-y-1">
        <div id="song-assist-file" class="font-mono text-zinc-200 truncate">video.mov</div>
        <div id="song-assist-artist" class="text-brand-400">Artist not selected</div>
      </div>
      <div class="bg-canvas border border-border rounded-xl p-3 flex items-center gap-3">
        <button type="button" id="song-assist-play" onclick="toggleSongAssistPlayback()" class="w-10 h-10 rounded-full bg-brand-600 hover:bg-brand-500 text-white flex items-center justify-center transition shadow-md shadow-brand-500/20" aria-label="Play audio clip">
          <span id="song-assist-play-icon" class="text-base leading-none">▶</span>
        </button>
        <div class="min-w-0 flex-1">
          <div id="song-assist-play-label" class="text-xs font-semibold text-zinc-100">Listen to this clip</div>
          <div id="song-assist-play-status" class="text-[10px] text-zinc-500 mt-0.5">Play the video audio while choosing a title.</div>
        </div>
        <audio id="song-assist-audio" preload="metadata" class="hidden"></audio>
      </div>
      <div class="space-y-2">
        <label for="song-assist-search" class="text-xs font-semibold text-zinc-300">Search song titles</label>
        <input type="search" id="song-assist-search" placeholder="Start typing a song title..." autocomplete="off" class="w-full bg-canvas border border-border rounded-xl px-3 py-2.5 text-sm text-zinc-200 placeholder-zinc-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500" oninput="searchSongOptions()" />
        <div id="song-assist-source" class="text-[10px] text-zinc-500"></div>
        <div id="song-options-container" class="max-h-56 overflow-y-auto space-y-1.5 pr-1">
          <div class="text-center py-5 text-zinc-500 text-xs">Loading song choices...</div>
        </div>
      </div>
      <div id="song-selected-preview" class="hidden p-3 rounded-xl bg-brand-500/10 border border-brand-500/30 text-xs space-y-1">
        <div class="text-[10px] font-bold uppercase tracking-wider text-brand-400">Selected Song</div>
        <div id="song-selected-name" class="font-bold text-white text-sm">Song title</div>
      </div>
      <div class="flex items-center justify-between pt-2 border-t border-border">
        <button type="button" onclick="closeSongAssistModal()" class="px-4 py-2 text-xs font-semibold rounded-xl bg-card border border-border text-zinc-300 hover:bg-zinc-800 transition">Cancel</button>
        <button type="button" id="btn-confirm-set-song" onclick="confirmSetSong()" disabled class="px-5 py-2 text-xs font-semibold rounded-xl bg-gradient-to-r from-brand-600 to-indigo-600 text-white shadow-md shadow-brand-500/20 disabled:opacity-50 disabled:pointer-events-none hover:from-brand-500 hover:to-indigo-500 transition flex items-center gap-2">
          Confirm Song
        </button>
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

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
    }

    function isUnidentifiedSong(v) {
      if (!v) return false;
      const c = v.concert;
      if (!c) return false;
      const ident = v.identification || {};

      // If user has already selected the band for this video, do not re-prompt
      if (ident.status === 'USER_ASSISTED_BAND' || ident.fingerprint_service === 'user_assisted_band') {
        return false;
      }

      const song = (ident.detected_song_title || '').trim().toLowerCase();
      const unassigned = !song || song === 'live performance' || song === 'unidentified clip' || song === 'unidentified song' || song === 'unknown' || song === 'unidentified' || song === 'instrumental' || song === 'clip';
      const lowConf = !ident.match_confidence || ident.match_confidence < 0.50;
      const artist = (ident.detected_artist || '').trim().toLowerCase();
      const artistUnknown = !artist || ['live artist', 'unknown artist', 'unknown'].includes(artist);

      const hasLineupOrHeadliner = Boolean(c.headliner_artist || (c.lineup_artists && c.lineup_artists.length > 0));
      return artistUnknown && (unassigned || lowConf) && hasLineupOrHeadliner;
    }

    function isSongUnmatched(v) {
      if (!v) return false;
      const ident = v.identification || {};
      const song = (ident.detected_song_title || '').trim().toLowerCase();
      const unresolved = !song || ['live performance', 'unidentified clip', 'unidentified song', 'unknown', 'unidentified', 'instrumental', 'clip'].includes(song);
      return unresolved || !ident.match_confidence || ident.match_confidence < 0.50;
    }

    function knownArtistForVideo(v) {
      const ident = v?.identification || {};
      const c = v?.concert || {};
      const artist = ident.detected_artist || c.headliner_artist || '';
      return ['live artist', 'unknown artist', 'unknown'].includes(artist.trim().toLowerCase()) ? '' : artist;
    }

    function getShowBands(v) {
      if (!v) return [];
      const c = v.concert;
      if (!c) return [];
      const list = [...(c.lineup_artists || [])];
      if (c.headliner_artist && !list.includes(c.headliner_artist)) {
        list.unshift(c.headliner_artist);
      }
      return list.length > 0 ? list : (c.headliner_artist ? [c.headliner_artist] : []);
    }

    let activeSongAssistVideoId = null;
    let selectedSongTitle = null;
    let songSearchTimer = null;

    function openSongAssistModal(id) {
      const v = videosData.find(item => item.id === id);
      if (!v) return;
      const artist = knownArtistForVideo(v);
      if (!artist) {
        openBandAssistModal(id);
        return;
      }
      activeSongAssistVideoId = id;
      selectedSongTitle = null;
      document.getElementById('song-assist-file').innerText = v.filename || 'video.mov';
      document.getElementById('song-assist-artist').innerText = artist;
      const audio = document.getElementById('song-assist-audio');
      audio.pause();
      audio.currentTime = 0;
      audio.src = `/api/video/${id}/audio`;
      document.getElementById('song-assist-play-icon').innerText = '▶';
      document.getElementById('song-assist-play-label').innerText = 'Listen to this clip';
      document.getElementById('song-assist-play-status').innerText = 'Play the video audio while choosing a title.';
      document.getElementById('song-assist-search').value = '';
      document.getElementById('song-selected-preview').classList.add('hidden');
      document.getElementById('btn-confirm-set-song').disabled = true;
      document.getElementById('song-options-container').innerHTML = '<div class="text-center py-5 text-zinc-500 text-xs">Loading song choices...</div>';
      document.getElementById('song-assist-modal').classList.remove('hidden');
      loadSongOptions('');
    }

    function closeSongAssistModal() {
      const modal = document.getElementById('song-assist-modal');
      if (modal) modal.classList.add('hidden');
      const audio = document.getElementById('song-assist-audio');
      if (audio) {
        audio.pause();
        audio.removeAttribute('src');
        audio.load();
      }
      activeSongAssistVideoId = null;
      selectedSongTitle = null;
    }

    async function toggleSongAssistPlayback() {
      const audio = document.getElementById('song-assist-audio');
      if (!audio) return;
      try {
        if (audio.paused) {
          await audio.play();
          document.getElementById('song-assist-play-icon').innerText = 'Ⅱ';
          document.getElementById('song-assist-play-label').innerText = 'Playing clip';
          document.getElementById('song-assist-play-status').innerText = 'Listen for the song, then choose its title below.';
        } else {
          audio.pause();
          document.getElementById('song-assist-play-icon').innerText = '▶';
          document.getElementById('song-assist-play-label').innerText = 'Listen to this clip';
        }
      } catch (err) {
        document.getElementById('song-assist-play-status').innerText = 'Audio could not be played for this video.';
      }
    }

    document.getElementById('song-assist-audio')?.addEventListener('ended', () => {
      document.getElementById('song-assist-play-icon').innerText = '▶';
      document.getElementById('song-assist-play-label').innerText = 'Listen again';
    });

    async function loadSongOptions(query) {
      if (!activeSongAssistVideoId) return;
      const container = document.getElementById('song-options-container');
      if (query.length > 0 && query.length < 2) {
        container.innerHTML = '<div class="text-center py-5 text-zinc-500 text-xs">Type at least 2 characters to search the discography.</div>';
        return;
      }
      try {
        const res = await fetch(`/api/video/${activeSongAssistVideoId}/song-options?q=${encodeURIComponent(query)}`);
        const data = await res.json();
        const sourceLabel = data.source === 'setlist' ? 'Show setlist' : data.source === 'musicbrainz' ? 'Band discography' : 'No song source available';
        document.getElementById('song-assist-source').innerText = sourceLabel;
        if (!data.songs?.length) {
          container.innerHTML = `<div class="text-center py-5 text-zinc-500 text-xs">${data.source === 'musicbrainz' ? 'No matching titles found.' : 'No song choices found.'}</div>`;
          return;
        }
        container.innerHTML = data.songs.map((song) => {
          const encodedTitle = encodeURIComponent(song.title).replace(/'/g, '%27');
          return `
          <button type="button" onclick="selectSongOption(decodeURIComponent('${encodedTitle}'))" class="w-full text-left p-2.5 rounded-xl bg-card-subtle hover:bg-card-hover border border-border hover:border-brand-500/50 cursor-pointer transition flex items-center justify-between gap-3 group">
            <span class="font-semibold text-xs text-zinc-100 group-hover:text-brand-300">${escapeHtml(song.title)}</span>
            <span class="text-[10px] text-zinc-500">Select</span>
          </button>
        `;
        }).join('');
      } catch (err) {
        container.innerHTML = '<div class="text-center py-5 text-rose-300 text-xs">Could not load song choices.</div>';
      }
    }

    function searchSongOptions() {
      clearTimeout(songSearchTimer);
      const query = document.getElementById('song-assist-search').value.trim();
      songSearchTimer = setTimeout(() => loadSongOptions(query), 250);
    }

    function selectSongOption(title) {
      selectedSongTitle = title;
      document.getElementById('song-selected-name').innerText = title;
      document.getElementById('song-selected-preview').classList.remove('hidden');
      document.getElementById('btn-confirm-set-song').disabled = false;
    }

    async function confirmSetSong() {
      if (!activeSongAssistVideoId || !selectedSongTitle) return;
      const btn = document.getElementById('btn-confirm-set-song');
      btn.disabled = true;
      btn.innerText = 'Saving...';
      try {
        const res = await fetch(`/api/video/${activeSongAssistVideoId}/set-song`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ song_title: selectedSongTitle, artist_name: knownArtistForVideo(videosData.find(item => item.id === activeSongAssistVideoId)) })
        });
        const data = await res.json();
        if (!data.success) throw new Error(data.error || 'Server error');
        const id = activeSongAssistVideoId;
        closeSongAssistModal();
        await loadData();
        openDrawer(id);
      } catch (err) {
        alert('Failed to save song: ' + err.message);
      } finally {
        btn.innerText = 'Confirm Song';
        btn.disabled = !selectedSongTitle;
      }
    }

    async function triggerSongMatching(id, button) {
      if (button) { button.disabled = true; button.innerText = 'Matching...'; }
      try {
        const res = await fetch(`/api/video/${id}/match`, { method: 'POST' });
        const data = await res.json();
        if (!data.success) throw new Error(data.error || 'Server error');
        await loadData();
      } catch (err) {
        alert('Song matching failed: ' + err.message);
      } finally {
        if (button) { button.disabled = false; button.innerText = 'Try Matching Again'; }
      }
    }

    function checkAutoPromptBandModal() {
      if (activeTab !== 'review') return;
      if (window._userDismissedBandModal) return;
      const modal = document.getElementById('band-assist-modal');
      if (modal && !modal.classList.contains('hidden')) return;
      const pending = (videosData || []).filter(v => v.processing_status === 'PENDING_REVIEW');
      const firstNeeding = pending.find(v => isUnidentifiedSong(v));
      if (firstNeeding) {
        console.log('[StageSync] Auto-prompting band selection modal for video:', firstNeeding.id, firstNeeding.filename);
        openBandAssistModal(firstNeeding.id);
        return;
      }
      const songModal = document.getElementById('song-assist-modal');
      if (songModal && !songModal.classList.contains('hidden')) return;
      const firstSongNeeding = pending.find(v => isSongUnmatched(v) && knownArtistForVideo(v));
      if (firstSongNeeding) {
        console.log('[StageSync] Auto-prompting song selection modal for video:', firstSongNeeding.id, firstSongNeeding.filename);
        openSongAssistModal(firstSongNeeding.id);
      }
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

        if (activeTab === 'review') {
          setTimeout(checkAutoPromptBandModal, 150);
        }
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
      const enteringReview = tab === 'review' && activeTab !== 'review';
      activeTab = tab;
      if (enteringReview) {
        window._userDismissedBandModal = false;
      }
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

      if (tab === 'review') {
        setTimeout(checkAutoPromptBandModal, 150);
      }
    }

    function setStatusFilter(filter) {
      activeStatusFilter = filter;
      const filters = ['ALL', 'PENDING_REVIEW', 'APPROVED', 'PUBLISHED', 'REJECTED', 'HIGH_CONF'];
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
        if (activeStatusFilter === 'ALL') return v.processing_status !== 'REJECTED';
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
      const filtered = getFilteredVideos().filter(v => v.processing_status !== 'REJECTED');

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
        const hasKnownVenue = v.venue && v.venue.name && !v.venue.name.startsWith('Venue near');
        const needsBandAssist = isUnidentifiedSong(v);

        let confBadge = `<span class="text-[10px] font-mono px-2 py-0.5 rounded-md bg-zinc-800 text-zinc-400">Score: ${conf}%</span>`;
        if (conf >= 80) {
          confBadge = `<span class="text-[10px] font-mono px-2 py-0.5 rounded-md bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-semibold">${conf}% Match</span>`;
        } else if (conf >= 50) {
          confBadge = `<span class="text-[10px] font-mono px-2 py-0.5 rounded-md bg-amber-500/10 text-amber-400 border border-amber-500/20 font-semibold">${conf}% Match</span>`;
        }

        return `
          <div class="bg-card border border-border rounded-xl p-3 hover:border-brand-500/50 hover:shadow-lg hover:shadow-brand-500/5 transition space-y-2.5 group">
            <!-- Thumbnail Frame -->
            <div class="relative w-full h-24 rounded-lg overflow-hidden border border-border/60 bg-black/40 cursor-pointer group/thumb" onclick="openVenueAssistModal('${v.id}')" title="Click to assist venue identification">
              <img src="/api/video/${v.id}/thumbnail" alt="" class="w-full h-full object-cover">
              <div class="absolute inset-0 bg-black/50 opacity-0 group-hover/thumb:opacity-100 flex items-center justify-center text-[10px] text-white font-semibold transition gap-1">
                <svg class="w-3.5 h-3.5 text-brand-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" /></svg>
                <span>Assist Venue</span>
              </div>
            </div>

            <button type="button" onclick="openDrawer('${v.id}')" class="w-full text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 rounded-lg space-y-2 block">
              <div class="flex items-center justify-between text-[11px]">
                <span class="font-mono text-zinc-400 truncate max-w-[130px]">${v.filename}</span>
                <span class="font-mono px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-300 text-[10px]">${durStr}</span>
              </div>

              <div>
                <div class="font-bold text-xs text-zinc-100 group-hover:text-brand-300 transition line-clamp-1">${artist}</div>
                <div class="text-xs text-brand-400 font-medium line-clamp-1">${song}</div>
              </div>

              <div class="text-[11px] text-zinc-400 flex items-center gap-1.5 truncate">
                <svg class="w-3.5 h-3.5 text-zinc-400 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z" /></svg>
                <span class="truncate ${!hasKnownVenue ? 'text-amber-400 italic' : ''}">${venue}</span>
              </div>
            </button>

            <div class="pt-2 border-t border-border/50 flex items-center justify-between gap-1">
              ${confBadge}
              <div class="flex items-center gap-1">
                ${needsBandAssist ? `
                  <button type="button" onclick="event.stopPropagation(); openBandAssistModal('${v.id}')" title="Which band is this?" class="px-2 py-0.5 rounded text-[10px] font-semibold bg-amber-500/10 hover:bg-amber-500/20 text-amber-400 border border-amber-500/20 transition">
                    Band?
                  </button>
                ` : ''}
                <button type="button" onclick="event.stopPropagation(); openVenueAssistModal('${v.id}')" title="Assist venue identification" class="px-2 py-0.5 rounded text-[10px] font-semibold bg-brand-500/10 hover:bg-brand-500/20 text-brand-400 border border-brand-500/20 transition">
                  Venue
                </button>
                ${colKey === 'review' ? `
                  <button type="button" onclick="quickApprove('${v.id}')" class="px-2.5 py-1 text-[11px] font-semibold rounded-lg bg-brand-600 hover:bg-brand-500 text-white transition">
                    Approve ✓
                  </button>
                ` : songUnmatched && knownArtistForVideo(v) ? `
                  <button onclick="openSongAssistModal('${v.id}')" class="text-xs text-rose-300 hover:text-white font-bold flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-rose-500/15 hover:bg-rose-500/25 border border-rose-500/40 transition">
                    <span>What song is this?</span>
                  </button>
                ` : `
                  <button type="button" onclick="openDrawer('${v.id}')" class="text-[10px] text-zinc-400 hover:text-white transition px-1">Inspect →</button>
                `}
              </div>
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

      const needingBandList = pending.filter(v => isUnidentifiedSong(v));
      const needingBandCount = needingBandList.length;
      const firstNeeding = needingBandList[0];

      let headerBanner = '';
      if (needingBandCount > 0 && firstNeeding) {
        headerBanner = `
          <div class="bg-gradient-to-r from-amber-500/20 via-amber-600/10 to-transparent border border-amber-500/40 rounded-2xl p-4 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 shadow-lg shadow-amber-500/5 mb-4">
            <div class="flex items-center gap-3">
              <div class="w-10 h-10 rounded-xl bg-amber-500/20 text-amber-400 border border-amber-500/30 flex items-center justify-center flex-shrink-0">
                <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3" /></svg>
              </div>
              <div>
                <div class="font-bold text-sm text-white flex items-center gap-2">
                  <span>Lineup Identification Required</span>
                  <span class="px-2 py-0.5 rounded-full text-[10px] bg-amber-500 text-black font-mono font-bold">${needingBandCount} Clip(s)</span>
                </div>
                <p class="text-xs text-zinc-300 mt-0.5">Concert is correlated, but the performing band must be selected from the show's roster.</p>
              </div>
            </div>
            <button type="button" onclick="openBandAssistModal('${firstNeeding.id}')" class="px-4 py-2 rounded-xl bg-gradient-to-r from-amber-500 to-amber-600 hover:from-amber-400 hover:to-amber-500 text-black font-bold text-xs shadow-md shadow-amber-500/25 flex items-center gap-1.5 transition flex-shrink-0 cursor-pointer">
              <span>🎸 Open Band Selection Pop-up</span>
              <span>→</span>
            </button>
          </div>
        `;
      }

      container.innerHTML = headerBanner + pending.map(v => {
        const c = v.concert || {};
        const ident = v.identification || {};
        const conf = ident.match_confidence ? Math.round(ident.match_confidence * 100) : 0;
        const artist = ident.detected_artist || c.headliner_artist || 'Unknown Artist';
        const song = ident.detected_song_title || 'Live Performance';
        const venue = v.venue ? `${v.venue.name}, ${v.venue.city}` : 'Unknown Venue';
        const date = c.event_date || (v.recorded_at_utc ? v.recorded_at_utc.substring(0, 10) : 'Unknown Date');
        const needsBandAssist = isUnidentifiedSong(v);
        const songUnmatched = isSongUnmatched(v);
        const showBands = needsBandAssist ? getShowBands(v) : [];

        return `
          <div class="bg-card border border-border rounded-2xl p-5 hover:border-zinc-700 transition space-y-4 shadow-sm">
            <div class="flex flex-col md:flex-row gap-4">
              
              <!-- Video Still Preview -->
              <div class="w-full md:w-44 h-28 rounded-xl overflow-hidden border border-border bg-black/40 flex-shrink-0 relative group cursor-pointer" onclick="${needsBandAssist ? `openBandAssistModal('${v.id}')` : songUnmatched && knownArtistForVideo(v) ? `openSongAssistModal('${v.id}')` : `openVenueAssistModal('${v.id}')`}">
                <img src="/api/video/${v.id}/thumbnail" alt="" class="w-full h-full object-cover">
                <div class="absolute inset-0 bg-black/75 opacity-0 group-hover:opacity-100 flex flex-col items-center justify-center gap-1 text-[11px] text-white font-semibold transition p-2 text-center">
                  ${needsBandAssist ? `
                    <svg class="w-4 h-4 text-amber-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3" /></svg>
                    <span class="text-amber-400 font-bold">Which band is this?</span>
                    <span class="text-[9px] text-zinc-300 font-normal">Click for Pop-up</span>
                  ` : songUnmatched && knownArtistForVideo(v) ? `
                    <svg class="w-4 h-4 text-rose-300" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 3-.895-3-2-3-.895-3-2zM9 10l12-3" /></svg>
                    <span class="text-rose-300 font-bold">What song is this?</span>
                    <span class="text-[9px] text-zinc-300 font-normal">Click to choose</span>
                  ` : `
                    <svg class="w-4 h-4 text-brand-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" /></svg>
                    <span>What venue is this?</span>
                  `}
                </div>
              </div>

              <!-- Video & Match Details -->
              <div class="flex-1 space-y-3">
                <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-border pb-2.5">
                  <div class="text-left space-y-0.5">
                    <div class="flex items-center gap-2">
                      <span class="text-xs font-mono text-zinc-400">${v.filename}</span>
                      ${needsBandAssist ? `
                        <button type="button" onclick="openBandAssistModal('${v.id}')" class="px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/40 hover:bg-amber-500 hover:text-black transition">
                          🎸 Select Band
                        </button>
                      ` : ''}
                      ${songUnmatched ? `
                        <button type="button" onclick="event.stopPropagation(); openSongAssistModal('${v.id}')" class="px-2 py-0.5 rounded text-[10px] font-bold bg-rose-500/15 text-rose-300 border border-rose-500/40 hover:bg-rose-500 hover:text-white transition">
                          What song?
                        </button>
                      ` : ''}
                    </div>
                    <button type="button" class="text-left cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 rounded p-0" onclick="openDrawer('${v.id}')">
                      <h3 class="text-base font-bold text-zinc-100 flex items-center gap-2 mt-0.5">
                        <span class="hover:text-brand-300 transition">${artist}</span>
                        <span class="text-zinc-400 font-normal">—</span>
                        <span class="text-brand-400 font-semibold">${song}</span>
                      </h3>
                    </button>
                  </div>
                  <div class="flex items-center gap-2">
                    ${songUnmatched ? `
                      <span class="text-xs px-2.5 py-1 rounded-lg bg-rose-500/10 text-rose-300 border border-rose-500/30 font-semibold">Song not identified</span>
                    ` : ''}
                    <span class="text-xs px-2.5 py-1 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-mono font-semibold">
                      ${conf}% Match
                    </span>
                    <span class="text-xs px-2.5 py-1 rounded-lg bg-amber-500/10 text-amber-400 border border-amber-500/20 font-semibold">
                      Pending Review
                    </span>
                  </div>
                </div>

                <div class="grid grid-cols-1 sm:grid-cols-3 gap-2.5 text-xs">
                  <div class="bg-canvas p-2.5 rounded-xl border border-border flex items-center justify-between">
                    <div class="truncate mr-2">
                      <span class="text-zinc-400 block text-[10px] uppercase font-semibold">Venue & Location</span>
                      <span class="font-medium text-zinc-200 mt-0.5 block truncate">${venue}</span>
                    </div>
                    <button type="button" onclick="event.stopPropagation(); openVenueAssistModal('${v.id}')" title="Assist venue identification" class="px-2 py-1 rounded-lg bg-brand-500/10 hover:bg-brand-500/20 text-brand-400 border border-brand-500/30 text-[10px] font-semibold flex items-center gap-1 flex-shrink-0">
                      <svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" /></svg>
                      Edit
                    </button>
                  </div>
                  <div class="bg-canvas p-2.5 rounded-xl border border-border">
                    <span class="text-zinc-400 block text-[10px] uppercase font-semibold">Show Date & Tour</span>
                    <span class="font-medium text-zinc-200 mt-0.5 block truncate">${date} ${c.tour_name ? '(' + c.tour_name + ')' : ''}</span>
                  </div>
                  <div class="bg-canvas p-2.5 rounded-xl border border-border font-mono">
                    <span class="text-zinc-400 block text-[10px] font-sans uppercase font-semibold">Video Specs</span>
                    <span class="font-medium text-zinc-200 mt-0.5 block">${formatDuration(v.duration_seconds)} • ${v.width && v.height ? `${v.width}x${v.height}` : 'HD'}</span>
                  </div>
                </div>

                ${needsBandAssist ? `
                  <div class="bg-amber-500/10 border border-amber-500/30 rounded-xl p-3 space-y-2">
                    <div class="flex items-center justify-between">
                      <span class="text-xs font-bold text-amber-400 flex items-center gap-1.5">
                        <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3" /></svg>
                        Which band is this? (Concert Matched • Song Unconfirmed)
                      </span>
                      <button type="button" onclick="openBandAssistModal('${v.id}')" class="px-2.5 py-1 text-xs text-black font-bold bg-amber-400 hover:bg-amber-300 rounded-lg shadow-sm flex items-center gap-1 transition cursor-pointer">
                        <span>🎸 Open Pop-up Selector</span>
                        <span>→</span>
                      </button>
                    </div>
                    <div class="flex flex-wrap gap-1.5 items-center">
                      <span class="text-[11px] text-zinc-400 mr-1">Select lineup artist:</span>
                      ${showBands.length > 0 ? showBands.map(b => `
                        <button type="button" onclick="quickSetBand('${v.id}', '${b.replace(/'/g, "\\'")}')" class="px-2.5 py-1 rounded-lg text-xs font-semibold bg-zinc-800 hover:bg-amber-500 hover:text-black text-zinc-200 border border-zinc-700 transition cursor-pointer">
                          ${b}
                        </button>
                      `).join('') : `
                        <button type="button" onclick="openBandAssistModal('${v.id}')" class="text-xs text-amber-400 underline">Enter opener / performer name</button>
                      `}
                    </div>
                  </div>
                ` : ''}
                ${songUnmatched ? `
                  <div class="bg-rose-500/10 border border-rose-500/30 rounded-xl p-3 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
                    <div>
                      <div class="text-xs font-bold text-rose-300">StageSync could not identify this song.</div>
                      <div class="text-[11px] text-zinc-400 mt-0.5">Try the matcher again or choose the title manually.</div>
                    </div>
                    <div class="flex items-center gap-2 flex-shrink-0">
                      <button type="button" onclick="triggerSongMatching('${v.id}', this)" class="px-2.5 py-1.5 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700 text-[11px] font-semibold transition">Try Matching Again</button>
                      ${knownArtistForVideo(v) ? `<button type="button" onclick="openSongAssistModal('${v.id}')" class="px-2.5 py-1.5 rounded-lg bg-brand-600 hover:bg-brand-500 text-white text-[11px] font-semibold transition">What song is this?</button>` : ''}
                    </div>
                  </div>
                ` : ''}
              </div>
            </div>

            <!-- Footer Action Bar -->
            <div class="flex items-center justify-between pt-2 border-t border-border/50">
              <div class="flex items-center gap-3">
                <button onclick="openDrawer('${v.id}')" class="text-xs text-zinc-400 hover:text-brand-300 flex items-center gap-1">
                  <span>View Full Metadata & Setlist</span>
                  <span>→</span>
                </button>
                ${needsBandAssist ? `
                  <button onclick="openBandAssistModal('${v.id}')" class="text-xs text-amber-300 hover:text-white font-bold flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-amber-500/20 hover:bg-amber-500/30 border border-amber-500/40 transition">
                    <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3" /></svg>
                    <span>Which band is this? (Pop-up)</span>
                  </button>
                ` : `
                  <button onclick="openVenueAssistModal('${v.id}')" class="text-xs text-brand-400 hover:text-brand-300 font-semibold flex items-center gap-1">
                    <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" /></svg>
                    <span>What venue is this?</span>
                  </button>
                `}
              </div>
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

      // Thumbnail Frame
      const drawerThumb = document.getElementById('drawer-thumbnail');
      if (drawerThumb) {
        drawerThumb.src = `/api/video/${v.id}/thumbnail?t=${Date.now()}`;
      }

      // Lineup band assistance
      const bandAssistSection = document.getElementById('drawer-band-assist-section');
      const bandChips = document.getElementById('drawer-band-chips');
      if (bandAssistSection && bandChips) {
        if (isUnidentifiedSong(v)) {
          bandAssistSection.classList.remove('hidden');
          const bands = getShowBands(v);
          const currentArtist = (ident.detected_artist || '').toLowerCase();
          bandChips.innerHTML = bands.map(b => {
            const isCurrent = (currentArtist === b.toLowerCase());
            return `
              <button type="button" onclick="quickSetBand('${v.id}', '${b.replace(/'/g, "\\'")}')" class="px-2.5 py-1 rounded-lg text-xs font-semibold transition ${
                isCurrent
                  ? 'bg-amber-500 text-black shadow-sm'
                  : 'bg-zinc-800 hover:bg-amber-500/20 text-zinc-200 hover:text-amber-300 border border-zinc-700 hover:border-amber-500/40'
              }">
                ${b}${isCurrent ? ' ✓' : ''}
              </button>
            `;
          }).join('');
        } else {
          bandAssistSection.classList.add('hidden');
        }
      }

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
    // USER-ASSIST VENUE SEARCH MODAL
    // =================================================================
    let activeVenueAssistVideoId = null;
    let venueSearchDebounceTimer = null;
    let selectedVenueData = null;

    function openVenueAssistModal(id) {
      if (!id && activeSelectedVideo) id = activeSelectedVideo.id;
      const v = videosData.find(item => item.id === id);
      if (!v) return;

      activeVenueAssistVideoId = id;
      selectedVenueData = null;
      const refreshButton = document.getElementById('venue-assist-refresh');
      if (refreshButton) refreshButton.dataset.frameIndex = '0';

      const thumbImg = document.getElementById('venue-assist-thumbnail');
      if (thumbImg) {
        thumbImg.src = `/api/video/${id}/thumbnail?frame=0&t=${Date.now()}`;
      }

      document.getElementById('venue-assist-filename-tag').innerText = v.filename || 'video.mov';
      const dur = formatDuration(v.duration_seconds);
      const dateStr = v.recorded_at_local ? v.recorded_at_local.substring(0, 10) : (v.recorded_at_utc ? v.recorded_at_utc.substring(0, 10) : '');
      document.getElementById('venue-assist-meta-tag').innerText = `${dur} • ${dateStr}`;

      const currentVenue = v.venue ? v.venue.name : '';
      const currentCity = v.venue ? v.venue.city : '';
      document.getElementById('venue-assist-subtitle').innerText = `Identify venue for ${v.filename} recorded on ${dateStr || 'unknown date'}`;

      const searchInput = document.getElementById('venue-search-input');
      const initialQuery = currentVenue && !currentVenue.startsWith('Venue near') ? currentVenue : (currentCity || '');
      searchInput.value = initialQuery;

      document.getElementById('venue-selected-preview').classList.add('hidden');
      document.getElementById('btn-confirm-set-venue').disabled = true;

      document.getElementById('venue-assist-modal').classList.remove('hidden');
      searchInput.focus();

      if (searchInput.value) {
        executeVenueSearch(searchInput.value, currentCity);
      } else {
        document.getElementById('venue-search-results').innerHTML = `
          <div class="text-center py-6 text-zinc-400 text-xs">Type a venue name above to search across local DB, Setlist.fm & OSM...</div>
        `;
      }
    }

    function closeVenueAssistModal() {
      document.getElementById('venue-assist-modal').classList.add('hidden');
      activeVenueAssistVideoId = null;
      selectedVenueData = null;
    }

    function refreshVideoStill(kind, button) {
      const id = kind === 'venue' ? activeVenueAssistVideoId : activeBandAssistVideoId;
      const image = document.getElementById(`${kind}-assist-thumbnail`);
      if (!id || !image || !button || button.disabled) return;

      const currentFrame = Number(button.dataset.frameIndex || '0');
      const nextFrame = currentFrame + 1;
      const previousSrc = image.src;
      button.disabled = true;
      button.classList.add('opacity-60');
      button.querySelector('span').innerText = '…';

      const nextImage = new Image();
      nextImage.onload = () => {
        image.src = nextImage.src;
        button.dataset.frameIndex = String(nextFrame);
        button.disabled = false;
        button.classList.remove('opacity-60');
        button.querySelector('span').innerText = '↻';
      };
      nextImage.onerror = () => {
        image.src = previousSrc;
        button.disabled = false;
        button.classList.remove('opacity-60');
        button.querySelector('span').innerText = '↻';
        alert('Could not extract another frame from this video.');
      };
      nextImage.src = `/api/video/${id}/thumbnail?frame=${nextFrame}&t=${Date.now()}`;
    }

    function handleVenueSearchInput(val) {
      clearTimeout(venueSearchDebounceTimer);
      venueSearchDebounceTimer = setTimeout(() => {
        const v = videosData.find(item => item.id === activeVenueAssistVideoId);
        const cityHint = v && v.venue ? v.venue.city : '';
        executeVenueSearch(val, cityHint);
      }, 300);
    }

    async function executeVenueSearch(query, city) {
      const resultsBox = document.getElementById('venue-search-results');
      if (!query || query.trim().length === 0) {
        resultsBox.innerHTML = `<div class="text-center py-6 text-zinc-400 text-xs">Type a venue name above to find matches...</div>`;
        return;
      }

      resultsBox.innerHTML = `
        <div class="text-center py-5 text-zinc-400 text-xs flex items-center justify-center gap-2">
          <svg class="w-4 h-4 text-brand-400 animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>
          <span>Searching local DB, Setlist.fm & OpenStreetMap...</span>
        </div>
      `;

      try {
        const params = new URLSearchParams({ q: query.trim() });
        if (city && city.trim()) params.append('city', city.trim());
        const res = await fetch(`/api/venues/search?${params.toString()}`);
        const data = await res.json();
        const venues = data.venues || [];

        if (venues.length === 0) {
          resultsBox.innerHTML = `
            <div class="p-4 rounded-xl bg-card-subtle border border-border text-center space-y-2">
              <p class="text-xs text-zinc-400">No matching venues found for "${query}".</p>
              <button type="button" onclick="selectCustomVenue('${query.replace(/'/g, "\\'")}')" class="px-3 py-1.5 text-xs font-semibold rounded-lg bg-zinc-800 hover:bg-zinc-700 text-brand-300 border border-zinc-700 transition">
                Use "${query}" as custom venue name
              </button>
            </div>
          `;
          return;
        }

        resultsBox.innerHTML = venues.map((ven, idx) => {
          const sourceBadges = (ven.sources || []).map(s => {
            if (s === 'local') return `<span class="px-1.5 py-0.5 rounded text-[9px] font-mono bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">DB</span>`;
            if (s === 'setlist_fm') return `<span class="px-1.5 py-0.5 rounded text-[9px] font-mono bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">Setlist.fm</span>`;
            return `<span class="px-1.5 py-0.5 rounded text-[9px] font-mono bg-sky-500/10 text-sky-400 border border-sky-500/20">OSM</span>`;
          }).join(' ');

          const addr = ven.formatted_address || `${ven.city}, ${ven.state || ven.country || 'US'}`;

          return `
            <div onclick="selectVenueCard(${idx})" class="p-2.5 rounded-xl bg-card-subtle hover:bg-card-hover border border-border hover:border-brand-500/50 cursor-pointer transition flex items-center justify-between gap-3 group">
              <div class="space-y-0.5 overflow-hidden text-left">
                <div class="flex items-center gap-2">
                  <span class="font-bold text-xs text-zinc-100 group-hover:text-brand-300 transition truncate">${ven.name}</span>
                  ${sourceBadges}
                </div>
                <div class="text-[11px] text-zinc-400 truncate">${addr}</div>
              </div>
              <button type="button" class="px-2.5 py-1 rounded-lg bg-zinc-800 group-hover:bg-brand-600 text-[11px] font-semibold text-zinc-300 group-hover:text-white transition flex-shrink-0">
                Select
              </button>
            </div>
          `;
        }).join('');

        window._lastVenueSearchResults = venues;
      } catch (err) {
        resultsBox.innerHTML = `<div class="text-center py-5 text-rose-400 text-xs">Error searching venues: ${err}</div>`;
      }
    }

    function selectVenueCard(idx) {
      const venues = window._lastVenueSearchResults || [];
      const ven = venues[idx];
      if (!ven) return;

      selectedVenueData = ven;
      document.getElementById('venue-selected-name').innerText = ven.name;
      document.getElementById('venue-selected-address').innerText = ven.formatted_address || `${ven.city}, ${ven.state || ven.country || 'US'}`;
      document.getElementById('venue-selected-preview').classList.remove('hidden');
      document.getElementById('btn-confirm-set-venue').disabled = false;
    }

    function selectCustomVenue(name) {
      const v = videosData.find(item => item.id === activeVenueAssistVideoId);
      const city = v && v.venue ? v.venue.city : 'Unknown City';
      selectedVenueData = {
        name: name,
        city: city,
        country: 'US',
        formatted_address: `${name}, ${city}`,
      };
      document.getElementById('venue-selected-name').innerText = name;
      document.getElementById('venue-selected-address').innerText = city;
      document.getElementById('venue-selected-preview').classList.remove('hidden');
      document.getElementById('btn-confirm-set-venue').disabled = false;
    }

    async function confirmSetVenue() {
      if (!activeVenueAssistVideoId || !selectedVenueData) return;

      const btn = document.getElementById('btn-confirm-set-venue');
      const origText = btn.innerHTML;
      btn.innerHTML = `<span>⏳ Correlating Concert...</span>`;
      btn.disabled = true;

      try {
        const payload = {
          venue_name: selectedVenueData.name,
          city: selectedVenueData.city,
          state_province: selectedVenueData.state,
          country_code: selectedVenueData.country || 'US',
          latitude: selectedVenueData.latitude,
          longitude: selectedVenueData.longitude,
          setlist_fm_venue_id: selectedVenueData.setlist_fm_venue_id,
          osm_id: selectedVenueData.osm_id,
          formatted_address: selectedVenueData.formatted_address,
        };

        const res = await fetch(`/api/video/${activeVenueAssistVideoId}/set-venue`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!data.success) {
          alert('Failed to set venue: ' + (data.error || 'Server error'));
          return;
        }

        const vidId = activeVenueAssistVideoId;
        closeVenueAssistModal();
        await loadData();

        // If drawer is open, refresh it
        if (activeSelectedVideo && activeSelectedVideo.id === vidId) {
          openDrawer(vidId);
        }
      } catch (err) {
        alert('Failed to update venue: ' + err);
      } finally {
        btn.innerHTML = origText;
        btn.disabled = false;
      }
    }

    // =================================================================
    // USER-ASSIST BAND SELECTION ("Which band is this?")
    // =================================================================
    let activeBandAssistVideoId = null;
    let selectedBandName = null;

    function openBandAssistModal(id) {
      if (!id && activeSelectedVideo) id = activeSelectedVideo.id;
      const v = videosData.find(item => item.id === id);
      if (!v) return;

      window._userDismissedBandModal = false;
      activeBandAssistVideoId = id;
      selectedBandName = null;
      const refreshButton = document.getElementById('band-assist-refresh');
      if (refreshButton) refreshButton.dataset.frameIndex = '0';

      const thumbImg = document.getElementById('band-assist-thumbnail');
      if (thumbImg) {
        thumbImg.src = `/api/video/${id}/thumbnail?frame=0&t=${Date.now()}`;
      }

      document.getElementById('band-assist-filename-tag').innerText = v.filename || 'video.mov';
      const dur = formatDuration(v.duration_seconds);
      const dateStr = v.recorded_at_local ? v.recorded_at_local.substring(0, 10) : (v.recorded_at_utc ? v.recorded_at_utc.substring(0, 10) : '');
      document.getElementById('band-assist-meta-tag').innerText = `${dur} • ${dateStr}`;

      const c = v.concert || {};
      const showTitle = c.tour_name ? `${c.headliner_artist || 'Concert'} — ${c.tour_name}` : (c.headliner_artist ? `${c.headliner_artist} Live` : 'Concert Show');
      document.getElementById('band-assist-show-title').innerText = showTitle;

      const venueText = v.venue ? `${v.venue.name} • ${v.venue.city || ''} • ${c.event_date || dateStr}` : (c.event_date || dateStr);
      document.getElementById('band-assist-show-venue').innerText = venueText;

      document.getElementById('band-custom-input').value = '';
      document.getElementById('band-selected-preview').classList.add('hidden');
      document.getElementById('btn-confirm-set-band').disabled = true;

      const bands = getShowBands(v);
      const container = document.getElementById('band-options-container');

      if (bands.length === 0) {
        container.innerHTML = `
          <div class="p-3 rounded-xl bg-card-subtle border border-border text-center text-xs text-zinc-400">
            No lineup artists found for this show. Type a band name below.
          </div>
        `;
      } else {
        const ident = v.identification || {};
        const currentArtist = ident.detected_artist || '';

        container.innerHTML = bands.map((band, idx) => {
          const isHeadliner = (c.headliner_artist && band.toLowerCase() === c.headliner_artist.toLowerCase());
          const isCurrent = (currentArtist && band.toLowerCase() === currentArtist.toLowerCase());

          return `
            <div id="band-opt-${idx}" onclick="selectBandOption('${band.replace(/'/g, "\\'")}', ${idx})" class="p-2.5 rounded-xl bg-card-subtle hover:bg-card-hover border border-border hover:border-amber-500/50 cursor-pointer transition flex items-center justify-between gap-3 group">
              <div class="flex items-center gap-2">
                <span class="w-6 h-6 rounded-lg bg-zinc-800 flex items-center justify-center text-xs font-mono font-bold text-zinc-400 group-hover:text-amber-400 group-hover:bg-amber-500/10 transition">
                  ${idx + 1}
                </span>
                <div>
                  <div class="font-bold text-xs text-zinc-100 group-hover:text-amber-300 transition flex items-center gap-2">
                    <span>${band}</span>
                    ${isHeadliner ? `<span class="px-1.5 py-0.5 rounded text-[9px] font-mono uppercase bg-amber-500/10 text-amber-400 border border-amber-500/20">Headliner</span>` : ''}
                    ${isCurrent ? `<span class="px-1.5 py-0.5 rounded text-[9px] font-mono uppercase bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">Current</span>` : ''}
                  </div>
                </div>
              </div>
              <button type="button" class="px-2.5 py-1 rounded-lg bg-zinc-800 group-hover:bg-amber-600 text-[11px] font-semibold text-zinc-300 group-hover:text-white transition flex-shrink-0">
                Select
              </button>
            </div>
          `;
        }).join('');
      }

      document.getElementById('band-assist-modal').classList.remove('hidden');
    }

    function closeBandAssistModal(manualDismiss = false) {
      const modal = document.getElementById('band-assist-modal');
      if (modal) modal.classList.add('hidden');
      activeBandAssistVideoId = null;
      selectedBandName = null;
      if (manualDismiss) {
        window._userDismissedBandModal = true;
      }
    }

    function selectBandOption(bandName, optIdx) {
      selectedBandName = bandName;
      document.getElementById('band-selected-name').innerText = bandName;
      document.getElementById('band-selected-preview').classList.remove('hidden');
      document.getElementById('btn-confirm-set-band').disabled = false;

      const container = document.getElementById('band-options-container');
      if (container) {
        const cards = container.children;
        for (let i = 0; i < cards.length; i++) {
          if (optIdx !== undefined && i === optIdx) {
            cards[i].classList.add('border-amber-500', 'bg-amber-500/10');
          } else {
            cards[i].classList.remove('border-amber-500', 'bg-amber-500/10');
          }
        }
      }
    }

    function selectCustomBandFromInput() {
      const input = document.getElementById('band-custom-input');
      const val = (input ? input.value : '').trim();
      if (!val) return;
      selectBandOption(val);
    }

    async function confirmSetBand() {
      if (!activeBandAssistVideoId || !selectedBandName) return;

      const btn = document.getElementById('btn-confirm-set-band');
      const origText = btn.innerHTML;
      btn.innerHTML = `<span>⏳ Updating Band...</span>`;
      btn.disabled = true;

      try {
        const res = await fetch(`/api/video/${activeBandAssistVideoId}/set-band`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ artist_name: selectedBandName })
        });
        const data = await res.json();
        if (!data.success) {
          alert('Failed to set band: ' + (data.error || 'Server error'));
          return;
        }

        const vidId = activeBandAssistVideoId;
        await loadData();

        // Check if there are other pending videos needing band identification
        const remaining = (videosData || []).filter(item => item.id !== vidId && item.processing_status === 'PENDING_REVIEW' && isUnidentifiedSong(item));
        if (remaining.length > 0) {
          openBandAssistModal(remaining[0].id);
        } else {
          closeBandAssistModal();
        }

        if (activeSelectedVideo && activeSelectedVideo.id === vidId) {
          openDrawer(vidId);
        }
      } catch (err) {
        alert('Failed to update band: ' + err);
      } finally {
        btn.innerHTML = origText;
        btn.disabled = false;
      }
    }

    async function quickSetBand(videoId, bandName) {
      try {
        const res = await fetch(`/api/video/${videoId}/set-band`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ artist_name: bandName })
        });
        const data = await res.json();
        if (!data.success) {
          alert('Failed to set band: ' + (data.error || 'Server error'));
          return;
        }
        await loadData();
        if (activeSelectedVideo && activeSelectedVideo.id === videoId) {
          openDrawer(videoId);
        }
      } catch (err) {
        alert('Failed to update band: ' + err);
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
        closeVenueAssistModal();
        closeBandAssistModal();
        closeSongAssistModal();
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
    app.router.add_get("/api/video/{id}/thumbnail", handle_video_thumbnail)
    app.router.add_get("/api/video/{id}/audio", handle_video_audio)
    app.router.add_get("/api/video/{id}/song-options", handle_song_options)
    app.router.add_get("/api/venues/search", handle_venue_search)
    app.router.add_post("/api/video/{id}/approve", handle_approve_video)
    app.router.add_post("/api/video/{id}/reject", handle_reject_video)
    app.router.add_post("/api/video/{id}/set-venue", handle_set_video_venue)
    app.router.add_post("/api/video/{id}/set-band", handle_set_video_band)
    app.router.add_post("/api/video/{id}/set-song", handle_set_video_song)
    app.router.add_post("/api/video/{id}/match", handle_trigger_video_match)
    app.router.add_post("/api/scan", handle_trigger_scan)
    app.router.add_post("/api/picker/create", handle_picker_create)
    app.router.add_post("/api/picker/poll", handle_picker_poll)
    app.router.add_post("/api/process", handle_trigger_process)
    app.router.add_post("/api/upload", handle_trigger_upload)

    return app


async def handle_index(request: web.Request) -> web.Response:
    """Serve the single-page dashboard HTML."""
    return web.Response(
        text=HTML_PAGE,
        content_type="text/html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


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


async def handle_video_thumbnail(request: web.Request) -> web.Response:
    """Return JPEG video frame still or fallback SVG."""
    db: Database = request.app[DB_KEY]
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]
    video = db.get_video(video_id)
    if not video:
        return web.Response(
            body=ThumbnailExtractor.get_fallback_svg(),
            content_type="image/svg+xml",
        )

    try:
        frame_index = max(0, int(request.query.get("frame", "0")))
    except ValueError:
        frame_index = 0

    cached_path = coordinator.thumbnail_extractor.get_cached_path(video.id, frame_index)
    if cached_path.exists() and cached_path.stat().st_size > 0:
        return web.FileResponse(cached_path)

    if video.file_path and Path(video.file_path).exists():
        try:
            thumb = coordinator.thumbnail_extractor.extract_thumbnail(
                video_path=video.file_path,
                output_path=cached_path,
                offset_seconds=_thumbnail_offset(video.duration_seconds, frame_index),
            )
            return web.FileResponse(thumb)
        except Exception as e:
            logger.warning("Failed to extract thumbnail for %s: %s", video.id, e)

    return web.Response(
        body=ThumbnailExtractor.get_fallback_svg(),
        content_type="image/svg+xml",
    )


async def handle_video_audio(request: web.Request) -> web.Response:
    """Stream the original video container so the browser can play its audio track."""
    db: Database = request.app[DB_KEY]
    video = db.get_video(request.match_info["id"])
    if not video or not video.file_path:
        return web.json_response({"error": "Video file not available"}, status=404)

    media_path = Path(video.file_path)
    if not media_path.exists() or not media_path.is_file():
        return web.json_response({"error": "Video file not available"}, status=404)

    content_type = video.mime_type or mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
    return web.FileResponse(
        media_path,
        headers={
            "Content-Type": content_type,
            "Content-Disposition": "inline",
        },
    )


def _thumbnail_offset(duration_seconds: Optional[float], frame_index: int) -> float:
    """Pick a deterministic alternate timestamp for the requested frame."""
    duration = max(0.0, float(duration_seconds or 0.0))
    if frame_index <= 0 or duration <= 1.0:
        return 1.0

    positions = (0.35, 0.60, 0.80, 0.95)
    position = positions[(frame_index - 1) % len(positions)]
    return min(duration - 0.05, max(0.0, duration * position))


async def handle_venue_search(request: web.Request) -> web.Response:
    """Search venues across local DB, Setlist.fm, and OpenStreetMap."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    query = request.query.get("q", "").strip()
    city = request.query.get("city", "").strip() or None
    limit = int(request.query.get("limit", 15))

    if not query:
        return web.json_response({"venues": [], "count": 0})

    try:
        results = coordinator.venue_search_engine.search(query=query, city=city, limit=limit)
        return web.json_response({"venues": results, "count": len(results)})
    except Exception as e:
        logger.exception("Venue search error for query '%s': %s", query, e)
        return web.json_response({"venues": [], "count": 0, "error": str(e)})


async def handle_set_video_venue(request: web.Request) -> web.Response:
    """Associate venue with video and re-run concert matching and setlist identification."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]

    body = await request.json() if request.can_read_body else {}
    venue_name = (body.get("venue_name") or "").strip()
    if not venue_name:
        return web.json_response({"success": False, "error": "Missing venue_name"}, status=400)

    city = body.get("city")
    state_province = body.get("state_province") or body.get("state")
    country_code = body.get("country_code") or body.get("country") or "US"
    lat_raw = body.get("latitude")
    lon_raw = body.get("longitude")
    latitude = float(lat_raw) if lat_raw is not None else None
    longitude = float(lon_raw) if lon_raw is not None else None
    setlist_fm_venue_id = body.get("setlist_fm_venue_id")
    osm_id = body.get("osm_id")
    formatted_address = body.get("formatted_address")

    try:
        video, concert, ident = coordinator.reidentify_video_with_venue(
            video_id=video_id,
            venue_name=venue_name,
            city=city,
            state_province=state_province,
            country_code=country_code,
            latitude=latitude,
            longitude=longitude,
            setlist_fm_venue_id=setlist_fm_venue_id,
            osm_id=osm_id,
            formatted_address=formatted_address,
        )
        return web.json_response({
            "success": True,
            "video": serialize_model(video),
            "concert": serialize_model(concert),
            "identification": serialize_model(ident),
        })
    except Exception as e:
        logger.exception("Failed to set venue for video %s: %s", video_id, e)
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_set_video_band(request: web.Request) -> web.Response:
    """Assign specific performing band to video from show lineup."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]

    body = await request.json() if request.can_read_body else {}
    artist_name = (body.get("artist_name") or "").strip()
    if not artist_name:
        return web.json_response({"success": False, "error": "Missing artist_name"}, status=400)

    song_title = body.get("song_title")

    try:
        video, concert, ident = coordinator.reidentify_video_with_band(
            video_id=video_id,
            artist_name=artist_name,
            song_title=song_title,
        )
        return web.json_response({
            "success": True,
            "video": serialize_model(video),
            "concert": serialize_model(concert),
            "identification": serialize_model(ident),
        })
    except Exception as e:
        logger.exception("Failed to set band for video %s: %s", video_id, e)
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_song_options(request: web.Request) -> web.Response:
    """Return setlist choices or discography search choices for manual matching."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]
    query = request.query.get("q", "")
    try:
        return web.json_response(coordinator.get_song_options(video_id, query))
    except ValueError as exc:
        return web.json_response({"error": str(exc), "songs": []}, status=404)
    except Exception as exc:
        logger.exception("Song option search failed for video %s: %s", video_id, exc)
        return web.json_response({"error": str(exc), "songs": []}, status=500)


async def handle_set_video_song(request: web.Request) -> web.Response:
    """Save the user's selected song title using the existing band re-identification path."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]
    body = await request.json() if request.can_read_body else {}
    song_title = (body.get("song_title") or "").strip()
    artist_name = (body.get("artist_name") or "").strip()
    if not song_title:
        return web.json_response({"success": False, "error": "Missing song_title"}, status=400)

    if not artist_name:
        video = request.app[DB_KEY].get_video(video_id)
        ident = request.app[DB_KEY].get_identification_by_video_id(video_id) if video else None
        concert = request.app[DB_KEY].get_concert(ident.concert_id) if ident and ident.concert_id else None
        candidate_artist = (ident.detected_artist if ident else "").strip()
        if candidate_artist.casefold() in {"live artist", "unknown artist", "unknown"}:
            candidate_artist = ""
        artist_name = candidate_artist or (concert.headliner_artist if concert else "")
    if not artist_name:
        return web.json_response({"success": False, "error": "Select the performing band first"}, status=400)

    try:
        video, concert, ident = coordinator.reidentify_video_with_band(
            video_id=video_id,
            artist_name=artist_name,
            song_title=song_title,
        )
        return web.json_response({
            "success": True,
            "video": serialize_model(video),
            "concert": serialize_model(concert),
            "identification": serialize_model(ident),
        })
    except Exception as exc:
        logger.exception("Failed to set song for video %s: %s", video_id, exc)
        return web.json_response({"success": False, "error": str(exc)}, status=500)


async def handle_trigger_video_match(request: web.Request) -> web.Response:
    """Re-run the existing local audio matching pipeline for one video."""
    coordinator: PipelineCoordinator = request.app[COORD_KEY]
    video_id = request.match_info["id"]
    video = request.app[DB_KEY].get_video(video_id)
    if not video:
        return web.json_response({"success": False, "error": "Video not found"}, status=404)
    try:
        updated = coordinator.process_video(video)
        return web.json_response({"success": True, "video": serialize_model(updated)})
    except Exception as exc:
        logger.exception("Manual song matching failed for video %s", video_id)
        return web.json_response({"success": False, "error": str(exc)}, status=500)


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
