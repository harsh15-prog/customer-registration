# Google Forms setup

1. Sign in as the Google account that should own the form and create a standalone [Apps Script project](https://script.google.com/home/projects/create).
2. Paste the contents of `Code.gs` into the script editor. Turn on **Show appsscript.json manifest file in editor** under **Project Settings**, then paste in the supplied manifest.
3. In **Project Settings → Script properties**, set `API_BASE_URL` to the production HTTPS origin and `GOOGLE_WEBHOOK_SECRET` to the same random value you configure on Vercel. Use a URL without a path.
4. Save and run `setupRegistrationForm` as the owner. Grant Google permission to create the form, install triggers and make HTTPS requests. Re-running setup as that owner reuses the same form and triggers. Keep the generated Script Properties.
5. The execution log contains the responder and owner editing URLs. Open the owner URL and confirm **Publish → Manage → General access** allows **Anyone with the link** as a responder. If your Workspace restricts public access, use an account authorized to publish an external form. Verify the link in a signed-out window.
6. Set Vercel's `GOOGLE_FORM_URL` to the full `https://docs.google.com/forms/.../viewform` responder URL. Redeploy so the public landing page can open it.
7. Submit a synthetic response through the real responder link. Check the Apps Script **Executions** view and the MySQL customer table. Run setup twice and verify that the owner and responder URLs stay the same and that the project has one form submission trigger and one clock trigger.

The form keeps the original response. A five-minute trigger reconciles newly stored responses and retries transient failures with increasing delays. The backend uses the Google response ID as its idempotency key. Google displays a receipt for the form submission before the API delivery is confirmed.

Failed deliveries create a `RETRY_<response-id>` Script Property containing the status, attempt count and next retry time. They contain no answers. Inspect warnings in **Executions**. For a permanent failure, investigate the referenced response in the owner-only Google Form. Once the cause is resolved, place the response ID in `MANUAL_RETRY_RESPONSE_ID` and run `retryOneRegistration`. A previously accepted response ID remains associated with its original data.
