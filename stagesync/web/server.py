"""StageSync Web Dashboard & REST Server.

Provides a modern, lightweight web interface to monitor video discovery,
review identified concert footage, inspect setlists, and approve YouTube uploads.
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
  <title>StageSync | Concert Video & YouTube Archival</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          colors: {
            brand: { 500: '#3b82f6', 600: '#2563eb' }
          }
        }
      }
    }
  </script>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .tabular { font-variant-numeric: tabular-nums; }
  </style>
</head>
<body class="bg-zinc-950 text-zinc-100 min-h-screen antialiased flex flex-col">

  <!-- Header -->
  <header class="border-b border-zinc-800 bg-zinc-900/60 backdrop-blur sticky top-0 z-50">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
      <div class="flex items-center gap-3">
        <div class="w-8 h-8 rounded-lg bg-blue-600 flex items-center justify-center font-bold text-white shadow-sm">
          SS
        </div>
        <div>
          <span class="font-bold text-lg tracking-tight">StageSync</span>
          <span class="text-xs text-zinc-400 ml-2 border border-zinc-700 px-2 py-0.5 rounded">v0.1.0</span>
        </div>
      </div>

      <div class="flex items-center gap-2">
        <button onclick="openScanModal()" id="btn-scan" class="px-3 py-1.5 text-xs font-medium rounded-md bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700 transition flex items-center gap-1.5">
          <span>📸</span> Scan Photos
        </button>
        <button onclick="triggerAction('process')" id="btn-process" class="px-3 py-1.5 text-xs font-medium rounded-md bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700 transition flex items-center gap-1.5">
          <span>🧠</span> Process AI
        </button>
        <button onclick="triggerAction('upload')" id="btn-upload" class="px-3 py-1.5 text-xs font-medium rounded-md bg-blue-600 hover:bg-blue-500 text-white shadow-sm transition flex items-center gap-1.5">
          <span>🚀</span> Upload Approved
        </button>
      </div>
    </div>
  </header>

  <!-- Main Container -->
  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 flex-1 w-full space-y-6">

    <!-- Metrics Grid -->
    <section class="grid grid-cols-2 md:grid-cols-4 gap-4" id="stats-grid">
      <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
        <div class="text-xs font-medium text-zinc-400">Total Cataloged</div>
        <div class="text-2xl font-bold tabular mt-1" id="stat-total">0</div>
      </div>
      <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
        <div class="text-xs font-medium text-yellow-400">Pending Review</div>
        <div class="text-2xl font-bold tabular text-yellow-400 mt-1" id="stat-pending">0</div>
      </div>
      <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
        <div class="text-xs font-medium text-blue-400">Approved for Upload</div>
        <div class="text-2xl font-bold tabular text-blue-400 mt-1" id="stat-approved">0</div>
      </div>
      <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
        <div class="text-xs font-medium text-emerald-400">Published to YouTube</div>
        <div class="text-2xl font-bold tabular text-emerald-400 mt-1" id="stat-published">0</div>
      </div>
    </section>

    <!-- Quota & Auth Notification Banner -->
    <section class="bg-zinc-900 border border-zinc-800 rounded-xl p-4 flex flex-wrap items-center justify-between gap-4 text-xs">
      <div class="flex items-center gap-4 flex-wrap">
        <div class="flex items-center gap-1.5">
          <span class="w-2 h-2 rounded-full" id="dot-photos"></span>
          <span class="text-zinc-300 font-medium">Google Photos:</span>
          <span id="text-photos" class="text-zinc-400">Checking...</span>
        </div>
        <div class="flex items-center gap-1.5">
          <span class="w-2 h-2 rounded-full" id="dot-yt"></span>
          <span class="text-zinc-300 font-medium">YouTube API:</span>
          <span id="text-yt" class="text-zinc-400">Checking...</span>
        </div>
        <div class="flex items-center gap-1.5">
          <span class="w-2 h-2 rounded-full" id="dot-setlist"></span>
          <span class="text-zinc-300 font-medium">Setlist.fm:</span>
          <span id="text-setlist" class="text-zinc-400">Checking...</span>
        </div>
      </div>
      <div class="text-zinc-400 tabular" id="text-quota">
        Daily Quota: 0 / 5 uploads today
      </div>
    </section>

    <!-- Tabs Navigation -->
    <div class="border-b border-zinc-800 flex gap-4">
      <button onclick="setTab('review')" id="tab-btn-review" class="py-2.5 px-3 text-sm font-semibold border-b-2 border-blue-500 text-blue-400">
        Review Queue (<span id="tab-count-review">0</span>)
      </button>
      <button onclick="setTab('catalog')" id="tab-btn-catalog" class="py-2.5 px-3 text-sm font-semibold border-b-2 border-transparent text-zinc-400 hover:text-zinc-200">
        Full Catalog (<span id="tab-count-catalog">0</span>)
      </button>
    </div>

    <!-- Review Queue View -->
    <section id="view-review" class="space-y-4">
      <div id="review-list" class="grid grid-cols-1 gap-4">
        <div class="text-center py-12 text-zinc-500 text-sm">Loading queue...</div>
      </div>
    </section>

    <!-- Full Catalog View -->
    <section id="view-catalog" class="hidden">
      <div class="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden">
        <table class="w-full text-left text-xs">
          <thead class="bg-zinc-800/50 text-zinc-400 uppercase font-medium border-b border-zinc-800">
            <tr>
              <th class="px-4 py-3">File / Date</th>
              <th class="px-4 py-3">Artist & Song</th>
              <th class="px-4 py-3">Venue / City</th>
              <th class="px-4 py-3">Confidence</th>
              <th class="px-4 py-3">Status</th>
              <th class="px-4 py-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody id="catalog-table-body" class="divide-y divide-zinc-800/60 font-mono">
            <tr><td colspan="6" class="text-center py-8 text-zinc-500 font-sans">No videos cataloged yet.</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- Unified Scan Modal -->
    <div id="scan-modal" class="hidden fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4">
      <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-6 max-w-lg w-full space-y-4 shadow-xl">
        <div class="flex items-center justify-between border-b border-zinc-800 pb-3">
          <div class="flex items-center gap-3">
            <h3 class="font-bold text-base text-zinc-100 flex items-center gap-2">
              <span>🔍</span> Discovery Scan
            </h3>
            <div class="flex bg-zinc-950 p-0.5 rounded border border-zinc-800 text-xs">
              <button type="button" id="scan-tab-btn-photos" onclick="setScanMode('photos')" class="px-2.5 py-1 rounded bg-zinc-800 text-zinc-200 font-medium">Google Photos</button>
              <button type="button" id="scan-tab-btn-local" onclick="setScanMode('local')" class="px-2.5 py-1 rounded text-zinc-400 hover:text-zinc-200 font-medium">Local Folder</button>
            </div>
          </div>
          <button onclick="closeScanModal()" class="text-zinc-500 hover:text-zinc-300 text-sm">✕</button>
        </div>

        <!-- Notification / Error Box -->
        <div id="scan-feedback-box" class="hidden text-xs"></div>

        <!-- Mode: Google Photos -->
        <div id="scan-mode-photos" class="space-y-3 text-xs">
          <div>
            <label class="block text-zinc-400 mb-1">Start Date</label>
            <input type="date" id="scan-start-date" class="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-2 text-zinc-200 focus:outline-none focus:border-blue-500 font-mono">
          </div>
          <div>
            <label class="block text-zinc-400 mb-1">End Date</label>
            <input type="date" id="scan-end-date" class="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-2 text-zinc-200 focus:outline-none focus:border-blue-500 font-mono">
          </div>
          <div class="flex items-center justify-between pt-1 text-zinc-400">
            <div class="flex items-center gap-1.5">
              <span class="text-[11px]">Presets:</span>
              <button type="button" onclick="setQuickDays(7)" class="px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 text-[10px]">7d</button>
              <button type="button" onclick="setQuickDays(30)" class="px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 text-[10px]">30d</button>
              <button type="button" onclick="setQuickDays(90)" class="px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 text-[10px]">90d</button>
              <button type="button" onclick="setQuickDays(365)" class="px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 text-[10px]">1y</button>
            </div>
            <label class="flex items-center gap-1.5 cursor-pointer">
              <input type="checkbox" id="scan-dry-run" class="rounded bg-zinc-950 border-zinc-800 text-blue-600">
              <span class="text-[11px]">Dry Run</span>
            </label>
          </div>

          <div class="pt-2 border-t border-zinc-800/60 flex items-center justify-between">
            <span class="text-[11px] text-zinc-500">Or select directly in Google Photos:</span>
            <button type="button" onclick="launchPickerSession()" id="btn-picker-launch" class="px-2.5 py-1 text-xs rounded bg-zinc-800 hover:bg-zinc-700 text-blue-400 border border-blue-900/50">
              Launch Photos Picker ↗
            </button>
          </div>
        </div>

        <!-- Mode: Local Directory -->
        <div id="scan-mode-local" class="hidden space-y-3 text-xs">
          <div>
            <label class="block text-zinc-400 mb-1">Local Directory Path</label>
            <input type="text" id="scan-local-dir" placeholder="/home/workspace/concert_videos" class="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-2 text-zinc-200 focus:outline-none focus:border-blue-500 font-mono">
            <p class="text-[11px] text-zinc-500 mt-1">Point to any folder containing concert video files (.mp4, .mov, .m4v).</p>
          </div>
        </div>

        <div class="flex items-center justify-end gap-2 pt-3 border-t border-zinc-800">
          <button onclick="closeScanModal()" class="px-3 py-1.5 text-xs rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300">Close</button>
          <button onclick="executeScan()" id="btn-run-scan" class="px-4 py-1.5 text-xs font-medium rounded bg-blue-600 hover:bg-blue-500 text-white shadow-sm">Start Scan</button>
        </div>
      </div>
    </div>

  </main>

  <script>
    let activeTab = 'review';
    let videosData = [];

    async function loadData() {
      try {
        const [resStatus, resVideos] = await Promise.all([
          fetch('/api/status').then(r => r.json()),
          fetch('/api/videos').then(r => r.json())
        ]);

        // Render Stats
        document.getElementById('stat-total').innerText = resStatus.total_videos;
        document.getElementById('stat-pending').innerText = resStatus.status_counts.PENDING_REVIEW || 0;
        document.getElementById('stat-approved').innerText = resStatus.status_counts.APPROVED || 0;
        document.getElementById('stat-published').innerText = resStatus.status_counts.PUBLISHED || 0;
        document.getElementById('tab-count-review').innerText = resStatus.status_counts.PENDING_REVIEW || 0;
        document.getElementById('tab-count-catalog').innerText = resStatus.total_videos;
        document.getElementById('text-quota').innerText = `Daily Quota: ${resStatus.uploads_today} / ${resStatus.max_daily_uploads} uploads used`;

        // Render Integrations
        updateDot('dot-photos', 'text-photos', resStatus.services.google_photos.authenticated);
        updateDot('dot-yt', 'text-yt', resStatus.services.youtube.authenticated);
        updateDot('dot-setlist', 'text-setlist', resStatus.services.setlist_fm.has_key);

        videosData = resVideos.videos;
        renderReviewQueue();
        renderCatalog();
      } catch (err) {
        console.error('Failed to load status', err);
      }
    }

    function updateDot(dotId, textId, isActive) {
      const dot = document.getElementById(dotId);
      const text = document.getElementById(textId);
      if (isActive) {
        dot.className = 'w-2 h-2 rounded-full bg-emerald-500 shadow-sm shadow-emerald-500/50';
        text.innerText = 'Connected';
        text.className = 'text-emerald-400 font-medium';
      } else {
        dot.className = 'w-2 h-2 rounded-full bg-zinc-600';
        text.innerText = 'Not Configured';
        text.className = 'text-zinc-500';
      }
    }

    function setTab(tab) {
      activeTab = tab;
      const btnReview = document.getElementById('tab-btn-review');
      const btnCatalog = document.getElementById('tab-btn-catalog');
      const viewReview = document.getElementById('view-review');
      const viewCatalog = document.getElementById('view-catalog');

      if (tab === 'review') {
        btnReview.className = 'py-2.5 px-3 text-sm font-semibold border-b-2 border-blue-500 text-blue-400';
        btnCatalog.className = 'py-2.5 px-3 text-sm font-semibold border-b-2 border-transparent text-zinc-400 hover:text-zinc-200';
        viewReview.classList.remove('hidden');
        viewCatalog.classList.add('hidden');
      } else {
        btnCatalog.className = 'py-2.5 px-3 text-sm font-semibold border-b-2 border-blue-500 text-blue-400';
        btnReview.className = 'py-2.5 px-3 text-sm font-semibold border-b-2 border-transparent text-zinc-400 hover:text-zinc-200';
        viewCatalog.classList.remove('hidden');
        viewReview.classList.add('hidden');
      }
    }

    function renderReviewQueue() {
      const pending = videosData.filter(v => v.processing_status === 'PENDING_REVIEW');
      const container = document.getElementById('review-list');
      if (pending.length === 0) {
        container.innerHTML = `
          <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-8 text-center">
            <div class="text-3xl mb-2">🎉</div>
            <div class="font-medium text-zinc-300">Review queue is empty!</div>
            <div class="text-xs text-zinc-500 mt-1">Scan Google Photos or run intelligence processing to identify new clips.</div>
          </div>
        `;
        return;
      }

      container.innerHTML = pending.map(v => {
        const c = v.concert || {};
        const ident = v.identification || {};
        const conf = ident.match_confidence ? (ident.match_confidence * 100).toFixed(0) + '%' : 'N/A';
        const artist = ident.detected_artist || c.headliner_artist || 'Unknown Artist';
        const song = ident.detected_song_title || 'Live Performance';
        const venue = v.venue ? `${v.venue.name}, ${v.venue.city}` : 'Unknown Venue';
        const date = c.event_date || (v.recorded_at_utc ? v.recorded_at_utc.substring(0, 10) : 'Unknown Date');

        return `
          <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-5 hover:border-zinc-700 transition space-y-4">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-zinc-800/60 pb-3">
              <div>
                <span class="text-xs font-mono text-zinc-500">${v.filename}</span>
                <h3 class="text-base font-bold text-zinc-100 flex items-center gap-2 mt-0.5">
                  <span>${artist}</span>
                  <span class="text-zinc-500 font-normal">—</span>
                  <span class="text-blue-400">${song}</span>
                </h3>
              </div>
              <div class="flex items-center gap-2">
                <span class="text-xs px-2.5 py-1 rounded bg-zinc-800 text-zinc-300 border border-zinc-700 font-mono">
                  Score: ${conf}
                </span>
                <span class="text-xs px-2.5 py-1 rounded bg-yellow-950 text-yellow-300 border border-yellow-800">
                  Pending Review
                </span>
              </div>
            </div>

            <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
              <div class="bg-zinc-950/60 p-2.5 rounded border border-zinc-800">
                <span class="text-zinc-500 block">Venue & City</span>
                <span class="font-medium text-zinc-200 mt-0.5 block">${venue}</span>
              </div>
              <div class="bg-zinc-950/60 p-2.5 rounded border border-zinc-800">
                <span class="text-zinc-500 block">Date & Tour</span>
                <span class="font-medium text-zinc-200 mt-0.5 block">${date} ${c.tour_name ? '(' + c.tour_name + ')' : ''}</span>
              </div>
              <div class="bg-zinc-950/60 p-2.5 rounded border border-zinc-800">
                <span class="text-zinc-500 block">Duration & Resolution</span>
                <span class="font-medium text-zinc-200 mt-0.5 block font-mono">${v.duration_seconds.toFixed(0)}s • ${v.width}x${v.height}</span>
              </div>
            </div>

            <div class="flex items-center justify-end gap-2 pt-2">
              <button onclick="rejectVideo('${v.id}')" class="px-3 py-1.5 text-xs font-medium rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300 border border-zinc-700 transition">
                Reject
              </button>
              <button onclick="approveVideo('${v.id}')" class="px-4 py-1.5 text-xs font-medium rounded bg-blue-600 hover:bg-blue-500 text-white shadow-sm transition">
                Approve & Queue Upload
              </button>
            </div>
          </div>
        `;
      }).join('');
    }

    function renderCatalog() {
      const tbody = document.getElementById('catalog-table-body');
      if (videosData.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center py-8 text-zinc-500 font-sans">No videos cataloged yet.</td></tr>';
        return;
      }

      tbody.innerHTML = videosData.map(v => {
        const c = v.concert || {};
        const ident = v.identification || {};
        const artist = ident.detected_artist || c.headliner_artist || '—';
        const song = ident.detected_song_title || '—';
        const venue = v.venue ? `${v.venue.name}` : '—';
        const conf = ident.match_confidence ? (ident.match_confidence * 100).toFixed(0) + '%' : '—';

        let badgeClass = 'bg-zinc-800 text-zinc-400';
        if (v.processing_status === 'APPROVED') badgeClass = 'bg-blue-950 text-blue-300 border border-blue-800';
        if (v.processing_status === 'PUBLISHED') badgeClass = 'bg-emerald-950 text-emerald-300 border border-emerald-800';
        if (v.processing_status === 'REJECTED') badgeClass = 'bg-rose-950 text-rose-300 border border-rose-800';
        if (v.processing_status === 'PENDING_REVIEW') badgeClass = 'bg-yellow-950 text-yellow-300 border border-yellow-800';

        return `
          <tr class="hover:bg-zinc-800/30 transition">
            <td class="px-4 py-3 font-mono">${v.filename}<div class="text-[10px] text-zinc-500 font-sans">${v.recorded_at_utc ? v.recorded_at_utc.substring(0, 10) : '—'}</div></td>
            <td class="px-4 py-3 font-sans font-medium text-zinc-200">${artist} <div class="text-xs text-blue-400 font-normal">${song}</div></td>
            <td class="px-4 py-3 font-sans text-zinc-300">${venue}</td>
            <td class="px-4 py-3 font-mono text-zinc-400">${conf}</td>
            <td class="px-4 py-3"><span class="px-2 py-0.5 rounded text-[10px] font-sans font-medium ${badgeClass}">${v.processing_status}</span></td>
            <td class="px-4 py-3 text-right font-sans">
              ${v.processing_status === 'PENDING_REVIEW' ? `
                <button onclick="approveVideo('${v.id}')" class="text-blue-400 hover:text-blue-300 font-medium">Approve</button>
              ` : '—'}
            </td>
          </tr>
        `;
      }).join('');
    }

    async function approveVideo(id) {
      try {
        await fetch(`/api/video/${id}/approve`, { method: 'POST' });
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
      const btn = document.getElementById(`btn-${action}`);
      const origText = btn.innerHTML;
      btn.innerHTML = '<span>⏳</span> Working...';
      btn.disabled = true;

      try {
        const res = await fetch(`/api/${action}`, { method: 'POST' });
        const data = await res.json();
        await loadData();
      } catch (err) {
        alert(`Action ${action} failed: ` + err);
      } finally {
        btn.innerHTML = origText;
        btn.disabled = false;
      }
    }

    function openScanModal() {
      document.getElementById('scan-modal').classList.remove('hidden');
    }

    function closeScanModal() {
      document.getElementById('scan-modal').classList.add('hidden');
    }

    let currentScanMode = 'photos';

    function setScanMode(mode) {
      currentScanMode = mode;
      const btnPhotos = document.getElementById('scan-tab-btn-photos');
      const btnLocal = document.getElementById('scan-tab-btn-local');
      const viewPhotos = document.getElementById('scan-mode-photos');
      const viewLocal = document.getElementById('scan-mode-local');
      document.getElementById('scan-feedback-box').classList.add('hidden');

      if (mode === 'photos') {
        btnPhotos.className = 'px-2.5 py-1 rounded bg-zinc-800 text-zinc-200 font-medium';
        btnLocal.className = 'px-2.5 py-1 rounded text-zinc-400 hover:text-zinc-200 font-medium';
        viewPhotos.classList.remove('hidden');
        viewLocal.classList.add('hidden');
      } else {
        btnLocal.className = 'px-2.5 py-1 rounded bg-zinc-800 text-zinc-200 font-medium';
        btnPhotos.className = 'px-2.5 py-1 rounded text-zinc-400 hover:text-zinc-200 font-medium';
        viewLocal.classList.remove('hidden');
        viewPhotos.classList.add('hidden');
      }
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
        } else {
          const startDate = document.getElementById('scan-start-date').value;
          const endDate = document.getElementById('scan-end-date').value;
          const dryRun = document.getElementById('scan-dry-run').checked;
          payload = {
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
          if (data.is_scope_error) {
            feedback.innerHTML = `
              <div class="bg-amber-950/40 border border-amber-800 text-amber-200 p-3 rounded space-y-1.5">
                <div class="font-bold flex items-center gap-1.5">
                  <span>⚠️</span> Google Photos Scope Notice (403 Forbidden)
                </div>
                <p>Google has restricted broad automated background searches on user photo libraries.</p>
                <div class="pt-1 flex items-center gap-2">
                  <button type="button" onclick="setScanMode('local')" class="px-2 py-1 rounded bg-amber-900/60 hover:bg-amber-900 text-amber-100 font-semibold text-[11px]">
                    Use Local Folder Tab 📁
                  </button>
                  <button type="button" onclick="launchPickerSession()" class="px-2 py-1 rounded bg-blue-900/60 hover:bg-blue-900 text-blue-100 font-semibold text-[11px]">
                    Launch Photos Picker 📸
                  </button>
                </div>
              </div>
            `;
          } else {
            feedback.innerHTML = `
              <div class="bg-red-950/40 border border-red-800 text-red-200 p-3 rounded">
                <div class="font-bold">Scan Error:</div>
                <div>${data.error || 'Failed to scan videos.'}</div>
              </div>
            `;
          }
          return;
        }

        closeScanModal();
        await loadData();
        alert(`Scan completed: Discovered ${data.scanned_count || 0} candidate video(s).`);
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
          alert('Failed to create Google Photos Picker session: ' + (data.error || 'Unknown error'));
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

    // Auto-refresh every 10 seconds
    loadData();
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
            "google_photos": {
                "authenticated": coordinator.photos_client.is_authenticated(),
            },
            "youtube": {
                "authenticated": coordinator.uploader.auth.is_authenticated(),
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
    limit = int(request.query.get("limit", 100))

    target_status = VideoStatus(status_param) if status_param else None
    videos = db.list_videos(status=target_status, limit=limit)

    results = []
    for v in videos:
        data = serialize_model(v)
        # Embed related venue & concert & identification
        if v.venue_id:
            venue = db.get_venue(v.venue_id)
            data["venue"] = serialize_model(venue)
        else:
            data["venue"] = None

        if v.concert_id:
            concert = db.get_concert(v.concert_id)
            data["concert"] = serialize_model(concert)
        else:
            data["concert"] = None

        ident = db.get_identification_by_video(v.id)
        data["identification"] = serialize_model(ident)

        upload = db.get_upload_by_video(v.id)
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
    if video.venue_id:
        v = db.get_venue(video.venue_id)
        data["venue"] = serialize_model(v)
    if video.concert_id:
        c = db.get_concert(video.concert_id)
        data["concert"] = serialize_model(c)

    ident = db.get_identification_by_video(video.id)
    data["identification"] = serialize_model(ident)

    upload = db.get_upload_by_video(video.id)
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
