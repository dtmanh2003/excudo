"use strict";
let preferredPanel='chat', reviewingChanges=false;
function updatePanelFocus() {
  document.body.dataset.visualFocus=state.busy&&!reviewingChanges?'chat':preferredPanel;
}
function setPanelFocus(panel,review=false) {
  preferredPanel=panel;reviewingChanges=review;updatePanelFocus();
}
