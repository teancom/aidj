# AI DJ integration deployment

## Home Assistant deployment

Use the Home Assistant MCP for live deployment and control. Do not SSH to Home Assistant for this integration.

After changing files in `custom_components/aidj`:

1. Run the local test suite and static checks.
2. Commit and push the change when deployment is requested.
3. Update `teancom/aidj` through HACS using the Home Assistant MCP.
4. **Always restart Home Assistant after every HACS update.** Do not use a config-entry reload as a substitute, even if HACS or the reload tool reports `require_restart: false`; this integration's Python code is not considered active until the full HA restart has completed. Use the Home Assistant MCP (`ha_restart(confirm=True)`).
5. Wait for Home Assistant to return, then independently verify that the `aidj` config entry is `loaded`, the expected entities/services are present, and playback/queue state is unchanged before testing the feature.
6. Only report the deployment as active after that post-restart verification.

A successful HACS download or config-entry reload does not prove that newly downloaded Python code is loaded. Never skip the full restart.
