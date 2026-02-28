## Authorization Flows

This section describes the authorization flows for both existing and new users. We will start with the existing user flow first, as it is simpler.

### Terminology and Site Structure

These flows will refer to the following terms:

1. Main Site - This is the URL that points to the top-level S3 bucket hosting the AgentScribe wiki. This URL points to a Cloudfront distribution.
2. UserId - This is the ID of a user as returned by Supabase. It will be an email address.
3. User Site - This is the URL that points to the individual users's sites on the AgentScribe wiki. A user can only have one site, and it is always refered to by a url path [MainSite]/[UserSite]. For example:

Main Site: d2uffddt57s4t6.cloudfront.net
UserId: knuth@runyolo.dev
User Site: knuth-home
Full URL to user site: https://d2uffddt57s4t6.cloudfront.net/knuth-home

4. The User Site is where the single page web application lives.
5. The Main Site has a single page web app in it, which is open to the public, with detailed information about how AgentScribe works. 

### Existing User Authorization Flow

1. User navigates to their user site, which will have a URL like this: https://d2uffddt57s4t6.cloudfront.net/knuth-home
2. If the user is not logged in, they do not see any content of their site. The upper right of the frontend should have a "Sign in" link. The main body of the site should have a "Sign in with Google" button.
3. Once a user has logged in, they can see the contents of their site, the chatbot, and the "Credentials" panel. The user is able to edit, save, and preview the site.
4. A user can only edit his own user site and all the pages below it. So this means the backend will need to inspect both the JWT token to make sure its valid and that the user is viewing or editing their own site.
5. The mapping between userID and site will be done in Supabase. There will be a user_site table which maps the internal Supabase User UUID to the name of their site. The site name will be specified in the new user authorization flow discussed below. [QUESTION: What is the best way to implement this in the frontend and Supabase?]
6. Calls to the backend API's must always check that a user is only viewing or editing their own site.

### New User Authorization Flow

1. First time users will either navigate to the top-level main site or to a user site (other than their own). If they navigate to the main site, there should be a link at the upper right saying "Sign In" and a button on the main page that says "Create your Free Site". Clicking on either of these first initiates the Google SSO flow with Supabase.
2. When a new user is created in Supabase as part of the SSO flow, the following needs to happen:
    a. The user is brought to a view in the Main Site single page app that prompts the user to enter a site name and select from 1 of 3 site themes. The site name should default to the everything to the left of the "@" in the userID in the JWT token. The three site themes to choose from are light, dark, and Yolo. There should be thumbnails of each theme. The Yolo theme should be based on the color scheme here (https://runyolo.dev)
    b. After making the required selections, the backend is called. It first must validate that the user site name is not already in use, by examining the existing prefixes in the top level of the main site s3 bucket. If it exists, the user must put in a diffferent value. This continues until a suitable site name is specified.
    c. The backend must then run the steps in the eixsting webhook method in the FastAPI. It needs to also create the user site name prefix in the main S3 bucket and build and deploy the single page website for the theme the user selected. [QUESTION: Can these three single page websites be built ahead of time and stage on S3 somewhere and just copied to the user site location? Or do they need to be built with npm run build in order to compile in site specific information]?
    
     

