require=function e(t,o,r){function s(i,u){if(!o[i]){if(!t[i]){var a="function"==typeof require&&require;if(!u&&a)return a(i,!0);if(n)return n(i,!0);var c=new Error("Cannot find module '"+i+"'");throw c.code="MODULE_NOT_FOUND",c}var d=o[i]={exports:{}};t[i][0].call(d.exports,function(e){var o=t[i][1][e];return s(o?o:e)},d,d.exports,e,t,o,r)}return o[i].exports}for(var n="function"==typeof require&&require,i=0;i<r.length;i++)s(r[i]);return s}({"builds/detail":[function(e,t,o){function r(e){var t=this;t.id=n.observable(e.id),t.command=n.observable(e.command),t.output=n.observable(e.output),t.exit_code=n.observable(e.exit_code||0),t.successful=n.observable(0===t.exit_code()),t.run_time=n.observable(e.run_time),t.is_showing=n.observable(!t.successful()),t.toggleCommand=function(){t.is_showing(!t.is_showing())},t.command_status=n.computed(function(){return t.successful()?"build-command-successful":"build-command-failed"})}function s(e){function t(){o.finished()||(i.getJSON("/api/v2/build/"+e.id+"/",function(e){o.state(e.state),o.state_display(e.state_display),o.date(e.date),o.success(e.success),o.error(e.error),o.length(e.length),o.commit(e.commit);var t;for(t in e.commands){var r=e.commands[t],s=n.utils.arrayFirst(o.commands(),function(e){return e.id===r.id});s||o.commands.push(r)}}),setTimeout(t,2e3))}var o=this,e=e||{};o.state=n.observable(e.state),o.state_display=n.observable(e.state_display),o.finished=n.computed(function(){return"finished"===o.state()}),o.date=n.observable(e.date),o.success=n.observable(e.success),o.error=n.observable(e.error),o.length=n.observable(e.length),o.commands=n.observableArray(e.commands),o.display_commands=n.computed(function(){var e,t=[],s=o.commands();for(e in s){var n=new r(s[e]);t.push(n)}return t}),o.commit=n.observable(e.commit),o.legacy_output=n.observable(!1),o.show_legacy_output=function(){o.legacy_output(!0)},t()}var n=e("knockout"),i=e("jquery");s.init=function(e,t){var o=new s(e),t=t||i("#build-detail")[0];return n.applyBindings(o,t),o},t.exports.BuildDetailView=s},{jquery:"jquery",knockout:"knockout"}]},{},[]);