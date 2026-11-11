import subprocess
import unittest
from pathlib import Path


class SongSuggestionsTests(unittest.TestCase):
    def test_clip_options_empty_errors_and_stale_responses(self):
        page = Path(__file__).resolve().parents[1] / "stagesync/web/index.html"
        script = r"""
const fs = require('node:fs'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('    async renderSuggestedMatches(query) {');
const end = source.indexOf('\n  },\n\n  // Screen 5', start);
const container = {textContent:'', innerHTML:''};
const document = {getElementById:()=>container};
const Store = {state:{currentVideoId:'first'}};
const pending = {};
let lastArgs;
const Api = {getSongOptions:(...args)=>{lastArgs=args;return new Promise((resolve,reject)=>pending[args[0]]={resolve,reject});}};
const escapeHtml = s=>s;
const controller = eval('({' + source.slice(start,end) + '})');
(async()=>{
 const first=controller.renderSuggestedMatches();
 Store.state.currentVideoId='second';
 const second=controller.renderSuggestedMatches();
 pending.second.resolve({songs:[{title:'Actual track',source:'setlist'}]}); await second;
 assert.match(container.innerHTML,/Actual track/);
 assert.doesNotMatch(container.innerHTML,/Harvest|Drapery|Dirge|%|checked/);
 pending.first.resolve({songs:[{title:'Wrong artist'}]}); await first;
 assert.doesNotMatch(container.innerHTML,/Wrong artist/);
 const search=controller.renderSuggestedMatches('Ghost');
 assert.deepEqual(lastArgs,['second','Ghost',true]);
 pending.second.resolve({songs:[{title:'Ghost',source:'musicbrainz'}]}); await search;
 assert.match(container.innerHTML,/Ghost/);
 assert.match(container.innerHTML,/MusicBrainz/);
 const empty=controller.renderSuggestedMatches();
 pending.second.resolve({songs:[],requires_query:true}); await empty;
 assert.match(container.textContent,/No setlist/);
 const error=controller.renderSuggestedMatches();
 pending.second.reject(new Error('offline')); await error;
 assert.match(container.textContent,/Could not load/);
 assert.match(source,/Controllers.approve.renderSuggestedMatches\(\);/);
})().catch(e=>{console.error(e);process.exit(1)});
"""
        subprocess.run(["node", "-e", script, str(page)], check=True)
