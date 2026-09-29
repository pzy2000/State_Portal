#import <AppKit/AppKit.h>
#import <objc/runtime.h>

static BOOL (*originalSetActivationPolicy)(id, SEL, NSApplicationActivationPolicy);
static void (*originalOrderWindow)(id, SEL, NSWindowOrderingMode, NSInteger);

static BOOL warptestSetActivationPolicy(
    id receiver, SEL selector, NSApplicationActivationPolicy ignoredPolicy)
{
    (void)ignoredPolicy;
    return originalSetActivationPolicy(
        receiver, selector, NSApplicationActivationPolicyAccessory);
}

static void warptestActivateIgnoringOtherApps(id receiver, SEL selector, BOOL ignored)
{
    (void)selector;
    (void)ignored;
    [receiver deactivate];
}

static void warptestActivate(id receiver, SEL selector)
{
    (void)selector;
    [receiver deactivate];
}

static BOOL warptestRunningApplicationActivate(id receiver, SEL selector, NSUInteger options)
{
    (void)receiver;
    (void)selector;
    (void)options;
    return NO;
}

static void warptestOrderOutInstead(id receiver, SEL selector, id sender)
{
    (void)selector;
    (void)sender;
    [receiver orderOut:nil];
}

static void warptestOrderOutRegardless(id receiver, SEL selector)
{
    (void)selector;
    [receiver orderOut:nil];
}

static void warptestLegacyOrderWindow(
    id receiver,
    SEL selector,
    NSWindowOrderingMode place,
    NSInteger relativeTo)
{
    if (place == NSWindowOut) {
        originalOrderWindow(receiver, selector, place, relativeTo);
        return;
    }
    originalOrderWindow(receiver, selector, NSWindowOut, 0);
}

__attribute__((constructor)) static void warptestInstallBackgroundGuard(void)
{
    @autoreleasepool {
        NSApplication *application = [NSApplication sharedApplication];
        [application setActivationPolicy:NSApplicationActivationPolicyAccessory];
        [application deactivate];

        Class applicationClass = [NSApplication class];
        Method setPolicy = class_getInstanceMethod(
            applicationClass, @selector(setActivationPolicy:));
        originalSetActivationPolicy = (void *)method_getImplementation(setPolicy);
        method_setImplementation(setPolicy, (IMP)warptestSetActivationPolicy);

        Method activateIgnoring = class_getInstanceMethod(
            applicationClass, @selector(activateIgnoringOtherApps:));
        if (activateIgnoring != NULL)
            method_setImplementation(
                activateIgnoring, (IMP)warptestActivateIgnoringOtherApps);

        Method activate = class_getInstanceMethod(applicationClass, @selector(activate));
        if (activate != NULL)
            method_setImplementation(activate, (IMP)warptestActivate);

        Method runningActivate = class_getInstanceMethod(
            [NSRunningApplication class], @selector(activateWithOptions:));
        if (runningActivate != NULL)
            method_setImplementation(
                runningActivate, (IMP)warptestRunningApplicationActivate);

        // Unity can re-show its progress, splash, and editor windows after an
        // external accessibility hide/minimize request. Keep this headed
        // process renderable while ensuring its own AppKit windows are ordered
        // out before they can remain on the operator desktop.
        Class windowClass = [NSWindow class];
        Method orderFront = class_getInstanceMethod(
            windowClass, @selector(orderFront:));
        if (orderFront != NULL)
            method_setImplementation(orderFront, (IMP)warptestOrderOutInstead);

        Method makeKeyAndOrderFront = class_getInstanceMethod(
            windowClass, @selector(makeKeyAndOrderFront:));
        if (makeKeyAndOrderFront != NULL)
            method_setImplementation(
                makeKeyAndOrderFront, (IMP)warptestOrderOutInstead);

        Method orderFrontRegardless = class_getInstanceMethod(
            windowClass, @selector(orderFrontRegardless));
        if (orderFrontRegardless != NULL)
            method_setImplementation(
                orderFrontRegardless, (IMP)warptestOrderOutRegardless);

        // Unity 2020's legacy progress window bypasses the orderFront family
        // and calls the primitive ordering selector directly. Enable this
        // broader interception only for the registered jynew launch; newer
        // Unity editors keep the narrower hooks above.
        const char *legacyOrderGuard = getenv(
            "WARPTEST_C1_UNITY_LEGACY_ORDER_WINDOW_GUARD");
        if (legacyOrderGuard != NULL
            && legacyOrderGuard[0] == '1'
            && legacyOrderGuard[1] == '\0') {
            Method orderWindow = class_getInstanceMethod(
                windowClass, @selector(orderWindow:relativeTo:));
            if (orderWindow != NULL) {
                originalOrderWindow = (void *)method_getImplementation(orderWindow);
                method_setImplementation(
                    orderWindow, (IMP)warptestLegacyOrderWindow);
            }
        }
    }
}
