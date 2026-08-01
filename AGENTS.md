# AI DJ integration deployment

## Home Assistant deployment

Use the Home Assistant MCP for live deployment and control. Do not SSH to Home Assistant for this integration.

After changing files in `custom_components/aidj`:

1. Run the local test suite and static checks.
2. Commit and push the change when deployment is requested.
3. Update `teancom/aidj` through HACS using the Home Assistant MCP.
4. Inspect the HACS result and the Home Assistant notification/state. If HACS reports `require_restart: true`, or Home Assistant says a restart is required, a config-entry reload is not sufficient—restart Home Assistant through the MCP (`ha_restart(confirm=True)`). This is especially important when adding a platform file or changing integration loading behavior.
5. Wait for Home Assistant to return, then independently verify that the `aidj` config entry is `loaded`, the expected entities/services are present, and playback/queue state is unchanged before testing the feature.
6. Only report the deployment as active after that post-restart verification.

A successful HACS download or config-entry reload does not prove that newly downloaded Python code is loaded. Never skip the restart check.
