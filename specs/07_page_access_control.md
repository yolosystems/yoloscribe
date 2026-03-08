## Access Control

This document describes how the AgentScribe wiki should implement access to control to individual wiki pages.
The glossary of terms used in this document is as follows:

1. "Main Site" - This is the page that exists at the top level of the S3 bucket. In the URL, it is the DNS name of the cloudfront distribution.
2. "User Site" - This is the page the exists in a subdirectory of the top level of the S3 bucket. When a user signs up for AgentWiki, this is the page that gets created for them. The path to this page is s3://s3_bucket_root/user_site
3. "Pages" - These are the child pages that live beneath user sites. The path to these are: s3://s3_bucket_root/user_site/child_page. Child pages can be nested - meaning child pages can have child pages, etc.

Access control needs to support the following modes:

1. Public - In this mode, the page is open to anybody with the URL, whether they have authenticated and have a beareer token in the header or not.
2. Private - In this mode, the page is only visible to the user that owns the user site where the page lives. This requires that the user be authenticated and it requires that the backend check to make sure the user site in the URL is the user site owned by that user.
3. Shared - In this mode, the page is visible to the owner and one or more other registered users of an AgentWiki installation. Only site owners can share pages within their site. The users the page is shared with are identified by their email addresses. When a user visits a page that has been shared with him, the backend must check the JWT token for the user email and match it to the list of emails the page has been shared with.

Here is the behavior of each access mode:

1. Public - "View" only. The frontend should not allow the user to edit the page.
2. Private - "Full Control" - Since only site owners can access private pages in their sites, they must be able to edit pages and create and run agents. Private is the default setting for any new page and any newly created top level site.
3. Shared - Shared users can either have "View" access, meaning they can interact with the page in a read-only basis only. Or they can have "Write" access, which means they can edit the markdown but they can't define or run agents on pages the do not own themselves.

If a user visits a page he does not have access to, he should be able to request it with a "request access" button. This will put a notification to the page owner that the user has requested access.

The main site is not owned by any user and always requires authentication. The main site exists as the entry point for a new user to provision their own user site, and uses should always be shown the auth-wall on this page if they haven't logged in.

### Implementation and Software Design

#### Settings

Each page will have a "settings" associated with it that will store the visibility of the page and the list of users the page has been shared with, if the access mode of "shared" has been selected.

QUESTIONS ABOUT SOFTWARE DESIGN
+ Should we store the settings in a markdown file next to a pages content.md file called "settings.md"? This way the user can just use markdown to configure the access control of a page. The advantage of this is that its simple and clean and follows the pattern used across AgentWiki of definingn the site in markdown. 

+ Or, would it be better to use something like DynamoDB to store page settings? Since the backend is going to need to pull the settings whenever a request to retrieve content comes in, would DynamoDB be faster and more scalable?

+ Or, regardless of whether we use DynamoDB or S3 to store the settings, should we implement an in-memory cache in the backend that is keyed by page path (i.e. knuth/blog/commnts) and the value is a JSON doc representing the settings? Page settigns aren't likely to change often, so caching makes sense. Also the settings themselves aren't going to take up much memory. The cache would be small, even with thousands or millions of items in it.

#### Notifications

Notifications for a user will be stored in a markdown file at the top leve of the user's site. It should work just like search results and be stored in the .user/notifications.md file. The backend will update this file when a user requests access to the page. The frontend should have a notifications icon on the upper right of top navigation bar that should indicate if there are notifications in the file. Clicking on this will load the notifications file in the markdown editor and the user can "clear" notifiations simply by deleting them from the file.

### FUTURE CAPABILITIES

DO NOT IMPLEMENT THIS NOW! Instead come up with a detailed implementaton plan and write it to spec/08_users_and_groups.md

We will want users of shared pages to eventually be able to have "Full Control" of those pages, meaning they can create, edit and run agents, as well as edit the content of the pages in the markdown editor. However, this is tricky because when an agent runs, it runs as the user visiting that page. If this is a shared user, the IAM policy associated with the K8S serviceaccount the agent-runner will run as won't have access to S3 locations other than the user site for that user.

Updating the IAM policy every time a page is shared with the user isn't going to be a scalable solution - IAM is not designed for high volume API calls to put policy docs in place. So I think the solution here is eventually to create the concept of a "group". For shared pages that require the shared users to have "Full Control" access, those users must first be placed into a group. When a group is created, an IAM role & policy for that group is provisioned, and the S3 resources in the policy will include all the user sites that group has access to. Page owners can still share pages to individual users, but they'll just have "Write" access. Shared users with "Full Control" have be put into a group first.

AgentWiki should also be able to support SSO with identity federation to providers that support the concept of groups. For example, AgentWiki should be able to use Microsoft Entre AD (or other SAML compatible system). This will allow users to be managed in groups in their LDAP directory entries, and those groups can be created automatically in AgentScribe when a user registers for the first time and the SAML claim is sent to the callback URL in the backend.

Question: Aside from SAML, what other SSO protocols support the concept of a group? AgentScribe should support this as well.

When coming up with the detailed implementation plan for this, make suggestions about what kind of database we will need to map users to groups. Favor "serverless" solutions (like DynamoDB and S3 tables) over ones that require running more infrastructure.

Also, the current implementation is tightly coupled with Supabase for SSO. For this future implementation plan to support groups, make the SSO modular, so that when somebody deploys an instance of AgentScribe they can choose to do SSO with any OAuth/JWT compatible provider, SAML provider, and any other commonly used SSO protocol.



