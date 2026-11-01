/* Phase 4 navigation state machine.  No DOM access: tests/test_presentation_guide.py
   drives this file in node, and workbench.js is the only thing that touches the page. */
(function (root) {
  'use strict';

  function clone(state) {
    return {
      source: state.source,
      chapter: state.chapter,
      started: state.started,
      frame: state.frame,
      mode: state.mode,
      cell: state.cell,
      scenario: state.scenario,
      playing: state.playing,
      visited: state.visited.slice()
    };
  }

  function create(options) {
    var sources = options.sources || {};
    var state = {
      source: options.source,
      chapter: -1,
      started: false,
      frame: 0,
      mode: 'side',
      cell: null,
      scenario: 'baseline',
      playing: false,
      visited: []
    };

    function bundle(name) {
      var value = sources[name];
      if (!value) throw new Error('unknown source ' + name);
      return value;
    }

    function clampFrame(value) {
      var n = bundle(state.source).n_frames;
      if (value < 0) return 0;
      if (value > n - 1) return n - 1;
      return value;
    }

    function hasCell(name, cellId) {
      return bundle(name).cell_ids.indexOf(cellId) >= 0;
    }

    function chapterTarget(index) {
      var chapters = bundle(state.source).guide.chapters;
      if (index < 0 || index >= chapters.length) return null;
      return chapters[index];
    }

    /* Every chapter jump applies its target and stops the clock, so the frame
       cannot drift away from the chapter the reader just opened. */
    function applyChapter(index) {
      var chapter = chapterTarget(index);
      if (!chapter) return false;
      var target = chapter.target;
      if (target.source && target.source !== state.source) {
        if (!sources[target.source]) return false;
        state.source = target.source;
        state.cell = target.cell && hasCell(target.source, target.cell) ? target.cell : null;
      }
      state.frame = clampFrame(target.frame || 0);
      state.mode = target.mode || 'side';
      if (target.cell) {
        state.cell = hasCell(state.source, target.cell) ? target.cell : null;
      }
      state.scenario = target.scenario || 'baseline';
      state.chapter = index;
      state.playing = false;
      if (state.visited.indexOf(index) < 0) state.visited.push(index);
      return true;
    }

    var api = {
      get: function () { return clone(state); },
      start: function () {
        state.started = true;
        return applyChapter(0);
      },
      next: function () {
        if (!state.started) return false;
        return applyChapter(state.chapter + 1);
      },
      prev: function () {
        if (!state.started) return false;
        return applyChapter(state.chapter - 1);
      },
      restart: function () {
        state.started = true;
        return applyChapter(0);
      },
      /* Leaving the guide keeps the analysis position; only the guide state resets. */
      exit: function () {
        state.started = false;
        state.chapter = -1;
        state.playing = false;
        return true;
      },
      gotoChapter: function (index) {
        state.started = true;
        return applyChapter(index);
      },
      /* Event navigation always pauses: the target must not be taken away. */
      jumpToEvent: function (eventId) {
        var events = bundle(state.source).events;
        for (var i = 0; i < events.length; i++) {
          if (events[i].id === eventId) {
            if (events[i].status !== 'ok' || events[i].frame === null) return false;
            state.frame = clampFrame(events[i].frame);
            state.playing = false;
            return true;
          }
        }
        return false;
      },
      setFrame: function (value) {
        state.frame = clampFrame(value);
        return state.frame;
      },
      setMode: function (mode) {
        if (mode !== 'side' && mode !== 'delta') return false;
        state.mode = mode;
        return true;
      },
      selectCell: function (cellId) {
        if (cellId === null || cellId === undefined) {
          state.cell = null;
          return true;
        }
        if (!hasCell(state.source, cellId)) return false;
        state.cell = cellId;
        return true;
      },
      setScenario: function (id) {
        var rows = bundle(state.source).scenarios.rows;
        for (var i = 0; i < rows.length; i++) {
          if (rows[i].id === id) {
            state.scenario = id;
            return true;
          }
        }
        return false;
      },
      /* Changing source is a navigation to that source's own page, not a
         re-render: a cell or scenario that does not exist there can never be
         carried across, because the next page starts from its own defaults. */
      sourceHref: function (name) {
        if (!sources[name]) return null;
        return '../' + name + '/workbench.html';
      },
      setPlaying: function (value) {
        state.playing = !!value;
        return state.playing;
      },
      togglePlaying: function () {
        state.playing = !state.playing;
        return state.playing;
      }
    };
    return api;
  }

  root.WorkbenchCore = { create: create };
})(typeof window !== 'undefined' ? window : this);
