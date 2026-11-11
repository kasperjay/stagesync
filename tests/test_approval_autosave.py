import subprocess
import unittest
from pathlib import Path


class ApprovalAutosaveTests(unittest.TestCase):
    def test_input_saves_in_order_and_keeps_clip_identity(self):
        source = Path(__file__).resolve().parents[1] / 'stagesync/web/index.html'
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const html = fs.readFileSync(process.argv[1], 'utf8');
assert.match(html, /id="edit-artist"[^>]*oninput="handleArtistChange\(\)"/);
assert.match(html, /id="edit-song"[^>]*oninput="handleSongChange\(\)"/);
const fields = {'edit-artist': {value:'New artist'}, 'edit-song':{value:'New song'}};
const calls = [];
const clip = {id:'one',artist:'Old artist',title:'Old song'};
const context = {
 Store:{state:{currentVideoId:'one',videos:[clip]}},
 document:{getElementById:id=>fields[id]},
 Controllers:{approve:{renderClips(){}}},
 showToast(){},
 Api:{setSong:async(id,body)=>{calls.push([id,body]);return {success:true};}}
};
vm.createContext(context);
vm.runInContext(html.slice(html.indexOf('var approvalSaveQueue'), html.indexOf('async function confirmApprovalAndNext')), context);
(async()=>{
 const first = context.handleArtistChange();
 fields['edit-song'].value='Final song';
 const second = context.handleSongChange();
 context.Store.state.currentVideoId='two';
 await second;
 assert.equal(calls.length,2);
 assert.equal(calls[0][0],'one');
 assert.equal(calls[1][0],'one');
 assert.equal(calls[1][1].song_title,'Final song');
 assert.equal(clip._persistedSong,'Final song');
 assert.equal(clip.title,'Final song');
})().catch(e=>{console.error(e);process.exit(1)});
'''
        result = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
